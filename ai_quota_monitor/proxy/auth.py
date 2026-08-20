from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


class AuthError(RuntimeError):
    """Raised when a usable access token cannot be obtained."""


class ClaudeTokenStore:
    """Reads and refreshes the Claude Code OAuth token.

    The token lives in ``~/.claude/.credentials.json`` under ``claudeAiOauth``.
    Access tokens are short-lived, so before every request we check expiry and
    use the refresh token to mint a new one. Refresh tokens rotate, so the new
    pair is written back to the same file — otherwise the real Claude Code CLI
    would be left holding an invalidated refresh token and forced to re-login.
    """

    # OAuth client id used by the Claude Code CLI (public, not a secret).
    CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
    # A real User-Agent is required; the default urllib one is blocked by
    # Cloudflare (error 1010) before the request ever reaches Anthropic.
    USER_AGENT = "claude-cli/2.1.226 (external, cli)"
    # Refresh this many seconds before the real expiry to avoid races.
    REFRESH_SKEW = 300

    def __init__(self, home: Path | None = None, timeout: float = 30.0) -> None:
        self.home = home or Path.home()
        self.timeout = timeout
        self._lock = threading.Lock()

    @property
    def credentials_path(self) -> Path:
        return self.home / ".claude" / ".credentials.json"

    def access_token(self) -> str:
        with self._lock:
            data = self._read()
            oauth = data.get("claudeAiOauth")
            if not isinstance(oauth, dict) or not oauth.get("accessToken"):
                raise AuthError(
                    "未找到 Claude Code 登录信息，请先运行 `claude` 并 /login"
                )
            expires_at = oauth.get("expiresAt")  # milliseconds since epoch
            expired = (
                isinstance(expires_at, (int, float))
                and expires_at / 1000 - self.REFRESH_SKEW <= time.time()
            )
            if expired and oauth.get("refreshToken"):
                oauth = self._refresh(data, oauth)
            return str(oauth["accessToken"])

    def subscription_type(self) -> str | None:
        try:
            oauth = self._read().get("claudeAiOauth") or {}
        except AuthError:
            return None
        value = oauth.get("subscriptionType")
        return str(value) if value else None

    def _read(self) -> dict:
        try:
            return json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AuthError(
                "未找到 Claude Code 登录信息，请先运行 `claude` 并 /login"
            ) from exc
        except (OSError, ValueError) as exc:
            raise AuthError(f"Claude 凭据读取失败：{exc}") from exc

    def _refresh(self, data: dict, oauth: dict) -> dict:
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": oauth["refreshToken"],
                "client_id": self.CLIENT_ID,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self.USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise AuthError(
                f"Claude 令牌刷新失败（HTTP {exc.code}），请重新 /login"
            ) from exc
        except urllib.error.URLError as exc:
            raise AuthError(f"Claude 令牌刷新网络错误：{exc.reason}") from exc

        oauth = dict(oauth)
        oauth["accessToken"] = payload["access_token"]
        if payload.get("refresh_token"):
            oauth["refreshToken"] = payload["refresh_token"]
        if payload.get("expires_in"):
            oauth["expiresAt"] = int((time.time() + float(payload["expires_in"])) * 1000)
        data["claudeAiOauth"] = oauth
        self._write(data)
        return oauth

    def _write(self, data: dict) -> None:
        path = self.credentials_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


class CodexTokenStore:
    """Reads and refreshes the Codex / ChatGPT OAuth token.

    Codex stores its token in ``$CODEX_HOME/auth.json`` under
    ``tokens.access_token`` with an ``tokens.refresh_token`` and a
    ``last_refresh`` timestamp. Access tokens are JWTs valid for ~1h; we refresh
    against the ChatGPT OAuth endpoint when the stored one is stale.
    """

    CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
    TOKEN_URL = "https://auth.openai.com/oauth/token"
    # Codex JWTs last about an hour; refresh once the stored token is older.
    MAX_AGE_SECONDS = 50 * 60

    def __init__(self, codex_home: Path, timeout: float = 30.0) -> None:
        self.codex_home = codex_home
        self.timeout = timeout
        self._lock = threading.Lock()

    @property
    def auth_path(self) -> Path:
        return self.codex_home / "auth.json"

    def access_token(self) -> str:
        with self._lock:
            data = self._read()
            tokens = data.get("tokens")
            if not isinstance(tokens, dict) or not tokens.get("access_token"):
                raise AuthError("未找到 Codex 登录信息，请先运行一次 Codex 登录")
            if self._is_stale(data) and tokens.get("refresh_token"):
                tokens = self._refresh(data, tokens)
            return str(tokens["access_token"])

    def account_id(self) -> str | None:
        try:
            tokens = self._read().get("tokens") or {}
        except AuthError:
            return None
        value = tokens.get("account_id")
        return str(value) if value else None

    def _is_stale(self, data: dict) -> bool:
        last = data.get("last_refresh")
        if not last:
            return True
        try:
            # last_refresh is an ISO-8601 string like "2026-01-02T03:04:05Z".
            from datetime import datetime

            stamp = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            return (time.time() - stamp.timestamp()) >= self.MAX_AGE_SECONDS
        except (ValueError, TypeError):
            return True

    def _read(self) -> dict:
        try:
            return json.loads(self.auth_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AuthError("未找到 Codex 登录信息，请先运行一次 Codex 登录") from exc
        except (OSError, ValueError) as exc:
            raise AuthError(f"Codex 凭据读取失败：{exc}") from exc

    def _refresh(self, data: dict, tokens: dict) -> dict:
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": self.CLIENT_ID,
                "scope": "openid profile email",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise AuthError(
                f"Codex 令牌刷新失败（HTTP {exc.code}），请重新登录 Codex"
            ) from exc
        except urllib.error.URLError as exc:
            raise AuthError(f"Codex 令牌刷新网络错误：{exc.reason}") from exc

        tokens = dict(tokens)
        tokens["access_token"] = payload["access_token"]
        if payload.get("refresh_token"):
            tokens["refresh_token"] = payload["refresh_token"]
        if payload.get("id_token"):
            tokens["id_token"] = payload["id_token"]
        data["tokens"] = tokens
        from datetime import datetime, timezone

        data["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write(data)
        return tokens

    def _write(self, data: dict) -> None:
        path = self.auth_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
