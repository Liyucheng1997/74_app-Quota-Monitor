from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class LimitWindow:
    label: str
    used_percent: float
    resets_at: datetime | None
    window_minutes: int | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))


@dataclass(slots=True)
class QuotaSnapshot:
    provider: str
    plan: str | None = None
    account_email: str | None = None
    windows: list[LimitWindow] = field(default_factory=list)
    credit_balance: str | None = None
    reset_credits_count: int | None = None
    reset_credit_grants: list[GrantBatch] = field(default_factory=list)
    reset_credit_details_available: bool = False
    source: str = ""
    sampled_at: datetime = field(default_factory=utc_now)
    error: str | None = None


@dataclass(slots=True)
class GrantBatch:
    count: int
    granted_at: datetime
    expires_at: datetime
    remaining: int | None = None
    source: str = "manual"
    estimated: bool = False
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.remaining is None:
            self.remaining = self.count

    @classmethod
    def observed(cls, count: int, now: datetime | None = None) -> "GrantBatch":
        now = now or utc_now()
        return cls(
            count=count,
            remaining=count,
            granted_at=now,
            expires_at=now + timedelta(days=30),
            source="auto",
            estimated=True,
        )

    @classmethod
    def from_backend_credit(cls, data: dict[str, Any]) -> "GrantBatch":
        granted_at = parse_datetime(data.get("granted_at"))
        expires_at = parse_datetime(data.get("expires_at"))
        if granted_at is None and expires_at is not None:
            granted_at = expires_at - timedelta(days=30)
        if expires_at is None and granted_at is not None:
            expires_at = granted_at + timedelta(days=30)
        granted_at = granted_at or utc_now()
        expires_at = expires_at or granted_at + timedelta(days=30)
        status = str(data.get("status") or "").lower()
        return cls(
            id=str(data.get("id") or uuid4().hex),
            count=1,
            remaining=1 if status == "available" else 0,
            granted_at=granted_at,
            expires_at=expires_at,
            source="backend",
            estimated=False,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["granted_at"] = self.granted_at.isoformat()
        data["expires_at"] = self.expires_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GrantBatch":
        return cls(
            id=str(data.get("id") or uuid4().hex),
            count=int(data["count"]),
            remaining=int(data.get("remaining", data["count"])),
            granted_at=parse_datetime(data["granted_at"]) or utc_now(),
            expires_at=parse_datetime(data["expires_at"]) or utc_now(),
            source=str(data.get("source", "manual")),
            estimated=bool(data.get("estimated", False)),
        )


@dataclass(slots=True)
class CodexAccount:
    name: str
    codex_home: str
    id: str = field(default_factory=lambda: uuid4().hex)
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CodexAccount":
        return cls(
            id=str(data.get("id") or uuid4().hex),
            name=str(data.get("name") or "Codex 账号"),
            codex_home=str(data["codex_home"]),
            is_default=bool(data.get("is_default", False)),
        )
