from __future__ import annotations

import glob
import json
import os
import queue
import re
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import GrantBatch, LimitWindow, QuotaSnapshot, parse_datetime


class ClaudeCollector:
    USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

    def __init__(self, home: Path | None = None, timeout: float = 10.0) -> None:
        self.home = home or Path.home()
        self.timeout = timeout

    def collect(self) -> QuotaSnapshot:
        credentials = self.home / ".claude" / ".credentials.json"
        try:
            auth = json.loads(credentials.read_text(encoding="utf-8"))["claudeAiOauth"]
            request = urllib.request.Request(
                self.USAGE_URL,
                headers=self._headers(auth["accessToken"]),
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
            windows = []
            for key, label in (("five_hour", "5 小时"), ("seven_day", "7 天")):
                item = payload.get(key)
                if item and item.get("utilization") is not None:
                    windows.append(
                        LimitWindow(
                            label=label,
                            used_percent=float(item["utilization"]),
                            resets_at=parse_datetime(item.get("resets_at")),
                        )
                    )
            extra = payload.get("extra_usage") or {}
            balance = None
            if extra.get("is_enabled") and extra.get("used_credits") is not None:
                balance = f"额外用量已用 {extra['used_credits']}"
            return QuotaSnapshot(
                provider="Claude Code",
                plan=auth.get("subscriptionType"),
                windows=windows,
                credit_balance=balance,
                source="Claude OAuth 实时数据",
            )
        except FileNotFoundError:
            return self._error("未找到 Claude Code 登录信息，请先运行 claude /login")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return self._error("Claude 用量请求被限流，将在稍后自动重试")
            if exc.code in (401, 403):
                return self._error(f"Claude 登录已失效（HTTP {exc.code}），请点击“登录 Claude”")
            return self._error(f"Claude 用量接口返回 HTTP {exc.code}")
        except urllib.error.URLError as exc:
            return self._error(f"Claude 网络请求失败：{exc.reason}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return self._error(f"Claude 数据读取失败：{exc}")

    @staticmethod
    def _error(message: str) -> QuotaSnapshot:
        return QuotaSnapshot(provider="Claude Code", source="不可用", error=message)

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "user-agent": self._claude_user_agent(),
        }

    @staticmethod
    def _claude_user_agent() -> str:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=3,
                creationflags=flags, check=False,
            )
            match = re.search(r"\d+\.\d+\.\d+", result.stdout or result.stderr)
            if match:
                return f"claude-code/{match.group(0)}"
        except (OSError, subprocess.SubprocessError):
            pass
        return "claude-code/2.1.183"


class CodexCollector:
    RESET_CREDITS_URL = "https://chatgpt.com/backend-api/codex/rate-limit-reset-credits"

    def __init__(
        self, home: Path | None = None, timeout: float = 10.0, codex_home: Path | None = None
    ) -> None:
        self.home = home or Path.home()
        self.codex_home = codex_home or self.home / ".codex"
        self.timeout = timeout

    def collect(self) -> QuotaSnapshot:
        executable = self._find_executable()
        if executable:
            try:
                return self._collect_rpc(executable)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                pass
        cached = self._collect_latest_session()
        if cached:
            return cached
        return QuotaSnapshot(
            provider="Codex",
            source="不可用",
            error="未找到 Codex CLI 或包含额度快照的会话，请先运行一次 Codex",
        )

    def _find_executable(self) -> Path | None:
        local = Path(os.environ.get("LOCALAPPDATA", self.home / "AppData" / "Local"))
        candidates = [Path(p) for p in glob.glob(str(local / "OpenAI" / "Codex" / "bin" / "*" / "codex.exe"))]
        candidates = [p for p in candidates if p.is_file()]
        return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

    def _collect_rpc(self, executable: Path) -> QuotaSnapshot:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(executable), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=flags,
            env={**os.environ, "CODEX_HOME": str(self.codex_home)},
        )
        output: queue.Queue[str] = queue.Queue()

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output.put(line)

        threading.Thread(target=read_stdout, daemon=True).start()

        def send(message: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()

        try:
            send({
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "ai-quota-monitor", "title": "AI Quota Monitor", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            })
            self._wait_for_id(output, 1)
            send({"method": "initialized", "params": {}})
            send({"id": 3, "method": "account/read", "params": {"refreshToken": False}})
            account_response = self._wait_for_id(output, 3)
            send({"id": 2, "method": "account/rateLimits/read", "params": {}})
            response = self._wait_for_id(output, 2)
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            result = response["result"]
            reset_count = (result.get("rateLimitResetCredits") or {}).get("availableCount")
            reset_credit_grants = []
            reset_credit_details_available = False
            reset_details = self._collect_reset_credit_details()
            if reset_details is not None:
                reset_count, reset_credit_grants = reset_details
                reset_credit_details_available = True
            snapshot = self._snapshot_from_rate_limits(
                result.get("rateLimits") or {},
                reset_count=reset_count,
                reset_credit_grants=reset_credit_grants,
                reset_credit_details_available=reset_credit_details_available,
                source="Codex 本地 RPC 实时数据",
            )
            account = (account_response.get("result") or {}).get("account") or {}
            snapshot.account_email = account.get("email")
            snapshot.plan = account.get("planType") or snapshot.plan
            return snapshot
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _collect_reset_credit_details(self) -> tuple[int | None, list[GrantBatch]] | None:
        auth_path = self.codex_home / "auth.json"
        try:
            token = json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]["access_token"]
            request = urllib.request.Request(
                self.RESET_CREDITS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "codex-cli/ai-quota-monitor",
                },
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
            grants = [
                GrantBatch.from_backend_credit(item)
                for item in payload.get("credits", [])
                if isinstance(item, dict)
            ]
            count = payload.get("available_count", payload.get("availableCount"))
            return int(count) if count is not None else None, grants
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
            return None

    def _wait_for_id(self, output: queue.Queue[str], request_id: int) -> dict[str, Any]:
        while True:
            try:
                message = json.loads(output.get(timeout=self.timeout))
            except queue.Empty as exc:
                raise RuntimeError("Codex RPC 响应超时") from exc
            if message.get("id") == request_id:
                return message

    def _collect_latest_session(self) -> QuotaSnapshot | None:
        root = self.codex_home / "sessions"
        if not root.exists():
            return None
        files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        for path in files:
            latest = None
            try:
                for line in path.open(encoding="utf-8", errors="ignore"):
                    if '"rate_limits"' not in line:
                        continue
                    message = json.loads(line)
                    payload = message.get("payload") or {}
                    if payload.get("type") == "token_count" and payload.get("rate_limits"):
                        latest = payload["rate_limits"]
                if latest:
                    return self._snapshot_from_rate_limits(latest, source="Codex 会话缓存（非实时）")
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _snapshot_from_rate_limits(
        limits: dict[str, Any],
        reset_count: int | None = None,
        reset_credit_grants: list[GrantBatch] | None = None,
        reset_credit_details_available: bool = False,
        source: str = "",
    ) -> QuotaSnapshot:
        windows = []
        aliases = (("primary", "5 小时"), ("secondary", "7 天"))
        for key, label in aliases:
            item = limits.get(key) or {}
            used = item.get("usedPercent", item.get("used_percent"))
            resets = item.get("resetsAt", item.get("resets_at"))
            minutes = item.get("windowDurationMins", item.get("window_minutes"))
            if used is not None:
                reset_time = datetime.fromtimestamp(float(resets), timezone.utc) if resets else None
                windows.append(LimitWindow(label, float(used), reset_time, int(minutes) if minutes else None))
        credits = limits.get("credits") or {}
        balance = credits.get("balance") if credits.get("hasCredits", credits.get("has_credits", False)) else None
        return QuotaSnapshot(
            provider="Codex",
            plan=limits.get("planType", limits.get("plan_type")),
            windows=windows,
            credit_balance=str(balance) if balance is not None else None,
            reset_credits_count=int(reset_count) if reset_count is not None else None,
            reset_credit_grants=reset_credit_grants or [],
            reset_credit_details_available=reset_credit_details_available,
            source=source,
        )
