from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class ProxyConfig:
    """Runtime configuration for the local proxy.

    Every field can be overridden with an ``AQM_PROXY_*`` environment variable so
    the proxy can be launched from the UI, a shortcut, or a shell without editing
    code.
    """

    host: str = field(default_factory=lambda: _env("AQM_PROXY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("AQM_PROXY_PORT", "8787")))

    # Optional shared secret. When set, clients must send it as a Bearer token
    # (Authorization header) or an ``x-api-key`` header. Empty means no auth,
    # which is only safe because the server binds to localhost by default.
    api_key: str = field(default_factory=lambda: _env("AQM_PROXY_KEY", ""))

    # How to reach Claude:
    #   "cli"   -> shell out to the official `claude -p` binary (safer, default)
    #   "oauth" -> impersonate Claude Code against api.anthropic.com directly
    claude_backend: str = field(
        default_factory=lambda: _env("AQM_CLAUDE_BACKEND", "cli").lower()
    )

    # Model used when a client asks for a non-Claude model id (e.g. "gpt-4o").
    # For the OAuth backend this is a full model id; for the CLI backend a bare
    # alias (sonnet/opus/fable) is also accepted.
    default_claude_model: str = field(
        default_factory=lambda: _env("AQM_DEFAULT_MODEL", "claude-sonnet-4-5")
    )
    # Default CLI model alias when the client model can't be mapped.
    default_cli_alias: str = field(
        default_factory=lambda: _env("AQM_CLI_MODEL", "sonnet")
    )

    # Fallback max_tokens for OpenAI-style requests that omit it (Anthropic
    # requires the field).
    default_max_tokens: int = field(
        default_factory=lambda: int(_env("AQM_DEFAULT_MAX_TOKENS", "4096"))
    )

    # Which Codex account (CODEX_HOME) to proxy. Defaults to ~/.codex.
    codex_home: Path = field(
        default_factory=lambda: Path(_env("CODEX_HOME", str(Path.home() / ".codex")))
    )
    claude_home: Path = field(default_factory=lambda: Path.home())

    upstream_timeout: float = field(
        default_factory=lambda: float(_env("AQM_PROXY_TIMEOUT", "600"))
    )

    def require_auth(self) -> bool:
        return bool(self.api_key)
