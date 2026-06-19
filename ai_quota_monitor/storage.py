from __future__ import annotations

import json
import os
from pathlib import Path

from .models import CodexAccount, GrantBatch


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AIQuotaMonitor"
        self.path = path or base / "state.json"

    def load_grants(self) -> list[GrantBatch]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [GrantBatch.from_dict(item) for item in data.get("codex_reset_grants", [])]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def save_grants(self, grants: list[GrantBatch]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "codex_reset_grants": [grant.to_dict() for grant in grants],
        }
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


class AccountStore:
    def __init__(self, path: Path | None = None) -> None:
        self.base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AIQuotaMonitor"
        self.path = path or self.base / "accounts.json"

    def load(self) -> list[CodexAccount]:
        default = CodexAccount(
            id="default",
            name="Codex 当前账号",
            codex_home=str(Path.home() / ".codex"),
            is_default=True,
        )
        if not self.path.exists():
            return [default]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = [CodexAccount.from_dict(item) for item in data.get("accounts", [])]
            if not any(account.is_default for account in accounts):
                accounts.insert(0, default)
            return accounts
        except (OSError, ValueError, TypeError, KeyError):
            return [default]

    def save(self, accounts: list[CodexAccount]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"schema_version": 1, "accounts": [a.to_dict() for a in accounts]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.path)

    def create(self, name: str) -> CodexAccount:
        account = CodexAccount(name=name, codex_home="")
        account.codex_home = str(self.base / "codex-profiles" / account.id)
        Path(account.codex_home).mkdir(parents=True, exist_ok=True)
        return account

    def grant_store(self, account: CodexAccount) -> StateStore:
        if account.is_default:
            return StateStore()
        return StateStore(self.base / f"grants-{account.id}.json")
