from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .auth import AuthError, ClaudeTokenStore, CodexTokenStore
from .cli import ClaudeCliBackend, collect_system, extract_files, prompt_with_files, render_prompt
from .config import ProxyConfig
from . import translate
from .upstream import ClaudeUpstream, CodexUpstream, UpstreamError


# CORS 放行名单：自家网页应用（liyucheng.me 各子域、GitHub Pages）与本机页面。
# 不放行 * —— 反代默认无鉴权，任意来源可用会让恶意网页偷跑订阅额度。
_CORS_ORIGIN_RE = re.compile(
    r"^https://([a-z0-9-]+\.)*liyucheng\.me$"
    r"|^https://liyucheng1997\.github\.io$"
    r"|^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
)


class _Handler(BaseHTTPRequestHandler):
    config: ProxyConfig
    claude: ClaudeUpstream
    claude_cli: ClaudeCliBackend
    codex: CodexUpstream

    server_version = "AIQuotaMonitorProxy"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------- #

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        print(f"[proxy] {self.address_string()} {fmt % args}")

    def _authorized(self) -> bool:
        if not self.config.require_auth():
            return True
        provided = ""
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            provided = header[len("Bearer "):].strip()
        provided = provided or self.headers.get("x-api-key", "").strip()
        return provided == self.config.api_key

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _cors_origin(self) -> str:
        origin = self.headers.get("Origin", "")
        return origin if _CORS_ORIGIN_RE.match(origin) else ""

    def _apply_cors(self) -> None:
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._apply_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str, err_type: str = "proxy_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": err_type}})

    def _start_stream(self) -> None:
        # No Content-Length is known for a stream, so under HTTP/1.1 we close the
        # connection at the end to unambiguously signal end-of-body (well-behaved
        # SDK clients also stop on the [DONE] / message_stop sentinel).
        self.close_connection = True
        self.send_response(200)
        self._apply_cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_chunk(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    # -- routing ---------------------------------------------------------- #

    def do_OPTIONS(self) -> None:  # noqa: N802 - required name
        """CORS 预检：浏览器网页(带 x-api-key 等头)调用前会先发 OPTIONS。"""
        origin = self._cors_origin()
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, x-api-key, anthropic-version, "
                "anthropic-dangerous-direct-browser-access",
            )
            # Chrome PNA:允许公网页面访问本机私有网络服务
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - required name
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/"):
            self._send_json(200, {"status": "ok", "service": "ai-quota-monitor-proxy"})
        elif path == "/v1/models":
            self._handle_models()
        else:
            self._send_error(404, f"未知路径：{path}", "not_found")

    def do_POST(self) -> None:  # noqa: N802 - required name
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            self._send_error(401, "缺少或错误的 API Key", "unauthorized")
            return
        try:
            body = self._read_json()
        except ValueError:
            self._send_error(400, "请求体不是合法 JSON", "invalid_request")
            return

        if path == "/v1/chat/completions":
            self._handle_chat_completions(body)
        elif path == "/v1/messages":
            self._handle_messages(body)
        elif path == "/codex/responses":
            self._handle_codex_responses(body)
        else:
            self._send_error(404, f"未知路径：{path}", "not_found")

    # -- handlers --------------------------------------------------------- #

    def _handle_models(self) -> None:
        models = [
            "claude-opus-4-1",
            "claude-sonnet-4-5",
            "claude-sonnet-4",
            "claude-3-5-haiku-latest",
        ]
        created = int(time.time())
        self._send_json(
            200,
            {
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "created": created, "owned_by": "anthropic"}
                    for m in models
                ],
            },
        )

    def _use_cli(self) -> bool:
        return self.config.claude_backend != "oauth"

    @staticmethod
    def _anthropic_system_text(system: Any) -> str:
        if isinstance(system, str):
            return system
        if isinstance(system, list):
            return "\n\n".join(
                b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"
            )
        return ""

    def _handle_chat_completions(self, body: dict) -> None:
        created = int(time.time())
        stream = bool(body.get("stream"))
        model = body.get("model") or self.config.default_claude_model
        messages = body.get("messages") or []

        if self._use_cli():
            system = collect_system(messages)
            files, cleanup = extract_files(messages, self.claude_cli.cwd)
            prompt = prompt_with_files(render_prompt(messages), files)
            tools = "Read" if files else ""
            try:
                if stream:
                    self._stream_openai_cli(system, prompt, model, created, tools)
                else:
                    try:
                        resp = self.claude_cli.complete(system, prompt, model, tools)
                    except UpstreamError as exc:
                        self._send_error(exc.status, exc.message, "upstream_error")
                        return
                    self._send_json(200, translate.anthropic_to_openai_response(resp, model, created))
            finally:
                cleanup()
            return

        try:
            payload = translate.openai_to_anthropic(
                body, self.config.default_claude_model, self.config.default_max_tokens
            )
            translate.inject_claude_code_identity(payload)
        except (ValueError, TypeError, KeyError) as exc:
            self._send_error(400, f"请求转换失败：{exc}", "invalid_request")
            return

        if stream:
            self._stream_openai(payload, model, created)
        else:
            payload["stream"] = False
            try:
                resp = self.claude.send(payload)
            except UpstreamError as exc:
                self._send_error(exc.status, exc.message, "upstream_error")
                return
            except AuthError as exc:
                self._send_error(401, str(exc), "auth_error")
                return
            self._send_json(200, translate.anthropic_to_openai_response(resp, model, created))

    def _stream_openai_cli(self, system: str, prompt: str, model: str, created: int, tools: str = "") -> None:
        try:
            events = self.claude_cli.stream_events(system, prompt, model, tools)
            self._start_stream()
            for chunk in translate.events_to_openai_chunks(events, model, created):
                self._write_chunk(chunk)
        except UpstreamError as exc:
            self._write_chunk(translate.error_chunk(exc.message, created, model))
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_openai(self, payload: dict, model: str, created: int) -> None:
        payload["stream"] = True
        try:
            lines = self.claude.stream(payload)
        except UpstreamError as exc:
            self._send_error(exc.status, exc.message, "upstream_error")
            return
        except AuthError as exc:
            self._send_error(401, str(exc), "auth_error")
            return
        self._start_stream()
        try:
            for chunk in translate.anthropic_stream_to_openai(lines, model, created):
                self._write_chunk(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        except UpstreamError as exc:
            self._write_chunk(translate.error_chunk(exc.message, created, model))

    def _handle_messages(self, body: dict) -> None:
        """Anthropic-native endpoint (OAuth: passthrough; CLI: emulated)."""

        stream = bool(body.get("stream"))
        model = body.get("model") or self.config.default_claude_model

        if self._use_cli():
            system = self._anthropic_system_text(body.get("system"))
            messages = body.get("messages") or []
            files, cleanup = extract_files(messages, self.claude_cli.cwd)
            prompt = prompt_with_files(render_prompt(messages), files)
            tools = "Read" if files else ""
            try:
                if stream:
                    try:
                        events = self.claude_cli.stream_events(system, prompt, model, tools)
                        self._start_stream()
                        for raw in translate.events_to_anthropic_sse(events):
                            self.wfile.write(raw)
                            self.wfile.flush()
                    except UpstreamError as exc:
                        self._send_error(exc.status, exc.message, "upstream_error")
                    except (BrokenPipeError, ConnectionResetError):
                        return
                else:
                    try:
                        resp = self.claude_cli.complete(system, prompt, model, tools)
                    except UpstreamError as exc:
                        self._send_error(exc.status, exc.message, "upstream_error")
                        return
                    self._send_json(200, resp)
            finally:
                cleanup()
            return

        translate.inject_claude_code_identity(body)
        if not body.get("model"):
            body["model"] = self.config.default_claude_model
        if not body.get("max_tokens"):
            body["max_tokens"] = self.config.default_max_tokens
        stream = bool(body.get("stream"))
        if stream:
            try:
                lines = self.claude.stream(body)
            except UpstreamError as exc:
                self._send_error(exc.status, exc.message, "upstream_error")
                return
            except AuthError as exc:
                self._send_error(401, str(exc), "auth_error")
                return
            self._start_stream()
            try:
                for line in lines:
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            try:
                resp = self.claude.send(body)
            except UpstreamError as exc:
                self._send_error(exc.status, exc.message, "upstream_error")
                return
            except AuthError as exc:
                self._send_error(401, str(exc), "auth_error")
                return
            self._send_json(200, resp)

    def _handle_codex_responses(self, body: dict) -> None:
        """Experimental raw passthrough to the ChatGPT Codex Responses backend."""

        try:
            lines = self.codex.stream(body)
        except UpstreamError as exc:
            self._send_error(exc.status, exc.message, "upstream_error")
            return
        except AuthError as exc:
            self._send_error(401, str(exc), "auth_error")
            return
        self._start_stream()
        try:
            for line in lines:
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


class ProxyServer:
    def __init__(self, config: ProxyConfig | None = None) -> None:
        self.config = config or ProxyConfig()
        claude_tokens = ClaudeTokenStore(self.config.claude_home)
        codex_tokens = CodexTokenStore(self.config.codex_home)
        handler = type(
            "BoundHandler",
            (_Handler,),
            {
                "config": self.config,
                "claude": ClaudeUpstream(claude_tokens, self.config.upstream_timeout),
                "claude_cli": ClaudeCliBackend(
                    default_alias=self.config.default_cli_alias,
                    timeout=self.config.upstream_timeout,
                ),
                "codex": CodexUpstream(codex_tokens, self.config.upstream_timeout),
            },
        )
        self.httpd = ThreadingHTTPServer((self.config.host, self.config.port), handler)

    @property
    def url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def serve(config: ProxyConfig | None = None) -> None:
    server = ProxyServer(config)
    cfg = server.config
    backend = "claude -p CLI（较安全）" if cfg.claude_backend != "oauth" else "OAuth 直连（较激进）"
    print(f"AI Quota Monitor 反代已启动：{server.url}")
    print(f"  Claude 后端      : {backend}")
    print(f"  OpenAI 兼容端点  : {server.url}/v1/chat/completions")
    print(f"  Anthropic 原生端点: {server.url}/v1/messages")
    print(f"  Codex(实验)      : {server.url}/codex/responses")
    if cfg.require_auth():
        print("  客户端需携带 API Key（Authorization: Bearer 或 x-api-key）")
    else:
        print("  未设置 API Key，仅监听本机；如需远程访问请设置 AQM_PROXY_KEY")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止反代……")
        server.shutdown()
