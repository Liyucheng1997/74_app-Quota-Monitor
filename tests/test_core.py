import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_quota_monitor.collectors import ClaudeCollector, CodexCollector
from ai_quota_monitor.models import GrantBatch, parse_datetime
from ai_quota_monitor.service import GrantService
from ai_quota_monitor.storage import AccountStore, StateStore
from ai_quota_monitor.ui import format_countdown, quota_bar_style


UTC = timezone.utc


class ModelTests(unittest.TestCase):
    def test_parse_datetime_normalizes_to_utc(self):
        value = parse_datetime("2026-06-19T18:00:00+02:00")
        self.assertEqual(value, datetime(2026, 6, 19, 16, 0, tzinfo=UTC))

    def test_countdown(self):
        now = datetime(2026, 6, 19, 10, 0, tzinfo=UTC)
        target = now + timedelta(days=1, hours=2, minutes=3)
        self.assertEqual(format_countdown(target, now), "后重置 · 1天 2小时 3分")

    def test_quota_bar_color_thresholds(self):
        self.assertEqual(quota_bar_style(79.9), "Safe.Horizontal.TProgressbar")
        self.assertEqual(quota_bar_style(80), "Warning.Horizontal.TProgressbar")
        self.assertEqual(quota_bar_style(99), "Warning.Horizontal.TProgressbar")
        self.assertEqual(quota_bar_style(100), "Danger.Horizontal.TProgressbar")



class CodexParsingTests(unittest.TestCase):
    def test_camel_case_rpc_payload(self):
        snapshot = CodexCollector._snapshot_from_rate_limits(
            {
                "primary": {"usedPercent": 47, "windowDurationMins": 300, "resetsAt": 1781890741},
                "secondary": {"usedPercent": 37, "windowDurationMins": 10080, "resetsAt": 1782415185},
                "credits": {"hasCredits": True, "balance": "12.5"},
                "planType": "plus",
            },
            reset_count=1,
        )
        self.assertEqual(snapshot.windows[0].remaining_percent, 53)
        self.assertEqual(snapshot.windows[1].window_minutes, 10080)
        self.assertEqual(snapshot.credit_balance, "12.5")
        self.assertEqual(snapshot.reset_credits_count, 1)

    def test_snake_case_session_payload(self):
        snapshot = CodexCollector._snapshot_from_rate_limits(
            {"primary": {"used_percent": 20, "window_minutes": 300, "resets_at": 1781890741}}
        )
        self.assertEqual(snapshot.windows[0].used_percent, 20)


class ClaudeCollectorTests(unittest.TestCase):
    def test_user_agent_tracks_installed_claude_code_version(self):
        result = type("Result", (), {"stdout": "2.1.183 (Claude Code)", "stderr": ""})()
        with patch("ai_quota_monitor.collectors.subprocess.run", return_value=result):
            self.assertEqual(ClaudeCollector._claude_user_agent(), "claude-code/2.1.183")

    def test_usage_headers_use_claude_code_identity(self):
        collector = ClaudeCollector()
        with patch.object(collector, "_claude_user_agent", return_value="claude-code/2.1.183"):
            headers = collector._headers("test-token")
        self.assertEqual(headers["user-agent"], "claude-code/2.1.183")


class GrantServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.json")
        self.service = GrantService(self.store)
        self.now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_live_count_creates_30_day_estimate(self):
        self.assertTrue(self.service.reconcile(2, self.now))
        grant = self.service.grants[0]
        self.assertEqual(grant.remaining, 2)
        self.assertEqual(grant.expires_at, self.now + timedelta(days=30))
        self.assertTrue(grant.estimated)

    def test_count_decrease_consumes_earliest_expiry(self):
        early = GrantBatch.observed(2, self.now)
        late = GrantBatch.observed(1, self.now + timedelta(days=1))
        self.service.grants = [late, early]
        self.store.save_grants(self.service.grants)
        self.service.reconcile(2, self.now)
        self.assertEqual(early.remaining, 1)
        self.assertEqual(late.remaining, 1)

    def test_manual_date_persists(self):
        self.service.add(1, self.now)
        loaded = StateStore(self.store.path).load_grants()
        self.assertEqual(loaded[0].granted_at, self.now)
        self.assertFalse(loaded[0].estimated)


class AccountStoreTests(unittest.TestCase):
    def test_default_and_created_profiles_use_distinct_homes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccountStore(Path(directory) / "accounts.json")
            accounts = store.load()
            self.assertEqual(accounts[0].id, "default")
            second = store.create("工作账号")
            accounts.append(second)
            store.save(accounts)
            loaded = store.load()
            self.assertEqual(len(loaded), 2)
            self.assertNotEqual(loaded[0].codex_home, loaded[1].codex_home)
            self.assertEqual(loaded[1].name, "工作账号")


if __name__ == "__main__":
    unittest.main()
