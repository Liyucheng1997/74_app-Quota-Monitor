from __future__ import annotations

from datetime import datetime, timedelta

from .models import GrantBatch, utc_now
from .storage import StateStore


class GrantService:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.grants = store.load_grants()

    def reconcile(
        self,
        available_count: int | None,
        now: datetime | None = None,
        backend_grants: list[GrantBatch] | None = None,
        backend_details_available: bool = False,
    ) -> bool:
        if backend_details_available:
            return self.sync_backend(backend_grants or [])
        if available_count is None:
            return False
        now = now or utc_now()
        tracked = sum(max(0, int(g.remaining or 0)) for g in self.grants)
        changed = False
        if available_count > tracked:
            self.grants.append(GrantBatch.observed(available_count - tracked, now))
            changed = True
        elif available_count < tracked:
            difference = tracked - available_count
            for grant in sorted(self.grants, key=lambda g: g.expires_at):
                take = min(int(grant.remaining or 0), difference)
                grant.remaining = int(grant.remaining or 0) - take
                difference -= take
                changed = changed or take > 0
                if difference == 0:
                    break
        if changed:
            self.store.save_grants(self.grants)
        return changed

    def sync_backend(self, backend_grants: list[GrantBatch]) -> bool:
        grants = sorted(backend_grants, key=lambda g: (g.expires_at, g.id))
        if [g.to_dict() for g in self.grants] == [g.to_dict() for g in grants]:
            return False
        self.grants = grants
        self.store.save_grants(self.grants)
        return True

    def add(self, count: int, granted_at: datetime) -> GrantBatch:
        grant = GrantBatch(
            count=count,
            remaining=count,
            granted_at=granted_at,
            expires_at=granted_at + timedelta(days=30),
            source="manual",
            estimated=False,
        )
        self.grants.append(grant)
        self.store.save_grants(self.grants)
        return grant

    def update_date(self, grant_id: str, granted_at: datetime) -> None:
        for grant in self.grants:
            if grant.id == grant_id:
                grant.granted_at = granted_at
                grant.expires_at = granted_at + timedelta(days=30)
                grant.estimated = False
                grant.source = "manual"
                self.store.save_grants(self.grants)
                return

    def delete(self, grant_id: str) -> None:
        self.grants = [g for g in self.grants if g.id != grant_id]
        self.store.save_grants(self.grants)
