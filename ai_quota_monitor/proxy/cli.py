"""Claude backend that shells out to the official ``claude -p`` CLI.

This is the safer default backend: instead of impersonating Claude Code against
the private OAuth inference endpoint (spoofed headers, hand-injected identity,
and us juggling the OAuth token file), we invoke the real Claude Code binary in
its documented non-interactive "print" mode. The request is genuinely Claude
Code, the CLI manages its own credentials (including Windows keychain + refresh),
and headless mode is an officially supported automation feature.

Trade-off: ``claude -p`` runs the Claude Code *agent*, not a bare model. We strip
it down with ``--tools ""`` and a caller-supplied ``--system-prompt`` so it
behaves like a plain chat model, but it is not a byte-faithful ``/v1/messages``
passthrough and does not surface OpenAI-style function calls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator

from .upstream import UpstreamError

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


def map_model(model: str | None, default_alias: str) -> str:
    """Map an incoming model name to a ``claude`` CLI model alias/name."""

    name = (model or "").lower()
    if not name:
        return default_alias
    if "opus" in name:
        return "opus"
    if "fable" in name:
        return "fable"
    if "haiku" in name:
        return "claude-haiku-4-5"
    if "sonnet" in name:
        return "sonnet"
    # A full claude-* id can be passed straight through; anything else
    # (gpt-4o, etc.) falls back to the configured default.
    if name.startswith("claude-"):
        return model  # type: ignore[return-value]
    return default_alias


def render_prompt(messages: list[dict]) -> str:
    """Flatten OpenAI-style messages (minus system) into a single prompt string."""

    turns: list[dict] = [m for m in messages if m.get("role") != "system"]

    def text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        return ""

    if len(turns) == 1 and turns[0].get("role") == "user":
        return text_of(turns[0].get("content"))
    labels = {"user": "User", "assistant": "Assistant", "tool": "Tool"}
    lines = []
    for message in turns:
        label = labels.get(message.get("role", "user"), "User")
        lines.append(f"{label}: {text_of(message.get('content'))}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


def collect_system(messages: list[dict]) -> str:
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
    return "\n\n".join(p for p in parts if p)


class ClaudeCliBackend:
    def __init__(
        self,
        default_alias: str = "sonnet",
        timeout: float = 600.0,
        cwd: Path | None = None,
        executable: str | None = None,
    ) -> None:
        self.default_alias = default_alias
        self.timeout = timeout
        self.executable = executable or shutil.which("claude")
        # Run in an empty directory so Claude Code does not auto-discover the
        # current project's CLAUDE.md / settings and inflate context and cost.
        self.cwd = cwd or (
            Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AIQuotaMonitor" / "proxy-cwd"
        )
        self.cwd.mkdir(parents=True, exist_ok=True)

    def available(self) -> bool:
        return bool(self.executable)

    def _base_args(self, model: str, system: str, output_format: str) -> list[str]:
        assert self.executable
        args = [
            self.executable,
            "-p",
            "--output-format",
            output_format,
            "--model",
            map_model(model, self.default_alias),
            "--tools",
            "",
            "--no-session-persistence",
            "--system-prompt",
            system or DEFAULT_SYSTEM_PROMPT,
        ]
        if output_format == "stream-json":
            args += ["--include-partial-messages", "--verbose"]
        return args

    def _popen(self, args: list[str]) -> subprocess.Popen:
        if not self.executable:
            raise UpstreamError(503, "未找到 claude CLI，请确认已安装 Claude Code")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )

    def complete(self, system: str, prompt: str, model: str) -> dict:
        """Run one non-streaming turn; return an Anthropic-shaped response dict."""

        from . import translate

        args = self._base_args(model, system, "json")
        process = self._popen(args)
        try:
            stdout, stderr = process.communicate(prompt, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise UpstreamError(504, "claude CLI 响应超时") from exc
        if process.returncode != 0:
            raise UpstreamError(502, (stderr or stdout or "claude CLI 执行失败").strip()[:500])
        try:
            data = json.loads(stdout)
        except ValueError as exc:
            raise UpstreamError(502, f"claude CLI 输出解析失败：{stdout[:300]}") from exc
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            raise UpstreamError(502, str(data.get("result") or data.get("api_error_status") or "claude CLI 返回错误"))
        return translate.build_anthropic_response(
            text=data.get("result") or "",
            stop_reason=data.get("stop_reason"),
            usage=data.get("usage") or {},
            model=map_model(model, self.default_alias),
            msg_id=data.get("session_id") or "cli",
        )

    def stream_events(self, system: str, prompt: str, model: str) -> Iterator[dict]:
        """Run one streaming turn; yield inner Anthropic event dicts."""

        args = self._base_args(model, system, "stream-json")
        process = self._popen(args)
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            process.kill()
            raise UpstreamError(502, f"claude CLI 写入失败：{exc}") from exc

        def drain_stderr() -> None:
            if process.stderr is not None:
                for _ in process.stderr:
                    pass

        threading.Thread(target=drain_stderr, daemon=True).start()
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("type") == "stream_event":
                    event = message.get("event")
                    if isinstance(event, dict):
                        yield event
        finally:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
