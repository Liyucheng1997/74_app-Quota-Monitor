from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator

from .auth import ClaudeTokenStore, CodexTokenStore


class UpstreamError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ClaudeUpstream:
    """Forwards Anthropic Messages requests using the Claude Code OAuth token."""

    MESSAGES_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"
    BETAS = "oauth-2025-04-20,claude-code-20250219,fine-grained-tool-streaming-2025-05-14"
    USER_AGENT = "claude-cli/2.1.226 (external, cli)"

    def __init__(self, tokens: ClaudeTokenStore, timeout: float = 600.0) -> None:
        self.tokens = tokens
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.access_token()}",
            "anthropic-version": self.ANTHROPIC_VERSION,
            "anthropic-beta": self.BETAS,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": self.USER_AGENT,
            "x-app": "cli",
        }

    def send(self, payload: dict) -> dict:
        """Non-streaming request; returns the parsed Anthropic response dict."""

        request = urllib.request.Request(
            self.MESSAGES_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise UpstreamError(exc.code, self._read_error(exc)) from exc
        except urllib.error.URLError as exc:
            raise UpstreamError(502, f"上游网络错误：{exc.reason}") from exc

    def stream(self, payload: dict) -> Iterator[bytes]:
        """Streaming request; yields raw SSE lines from the Anthropic response."""

        request = urllib.request.Request(
            self.MESSAGES_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise UpstreamError(exc.code, self._read_error(exc)) from exc
        except urllib.error.URLError as exc:
            raise UpstreamError(502, f"上游网络错误：{exc.reason}") from exc

        def iterator() -> Iterator[bytes]:
            with response:
                for line in response:
                    yield line

        return iterator()

    @staticmethod
    def _read_error(exc: urllib.error.HTTPError) -> str:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best effort error text
            detail = ""
        return detail or exc.reason or "上游错误"


class CodexUpstream:
    """Forwards requests to the ChatGPT backend used by Codex (experimental).

    The ChatGPT Codex backend speaks the OpenAI *Responses* protocol, not Chat
    Completions, and its request shape is undocumented and changes often. This
    class is a best-effort passthrough for the Responses format; Claude is the
    tested path.
    """

    RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

    def __init__(self, tokens: CodexTokenStore, timeout: float = 600.0) -> None:
        self.tokens = tokens
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.tokens.access_token()}",
            "content-type": "application/json",
            "accept": "text/event-stream",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "user-agent": "codex_cli_rs",
        }
        account = self.tokens.account_id()
        if account:
            headers["chatgpt-account-id"] = account
        return headers

    def stream(self, payload: dict) -> Iterator[bytes]:
        request = urllib.request.Request(
            self.RESPONSES_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                detail = exc.reason or ""
            raise UpstreamError(exc.code, detail or "Codex 上游错误") from exc
        except urllib.error.URLError as exc:
            raise UpstreamError(502, f"Codex 网络错误：{exc.reason}") from exc

        def iterator() -> Iterator[bytes]:
            with response:
                for line in response:
                    yield line

        return iterator()
