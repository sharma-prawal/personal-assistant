import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_HOME = Path("/private/tmp/jarvis-test-home")
TEST_HOME.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(TEST_HOME)
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("GROQ_API_KEY", "test-key")

import reliability
from browser_agent import _is_auth_wall, _parse_outlook_calendar_time
from main import _delta


class FakeAuthShellPage:
    url = "https://teams.microsoft.com/v2/#/conversations"

    async def title(self):
        return "Microsoft Teams"

    async def inner_text(self, selector):
        return ""

    async def evaluate(self, script):
        return "Sign in with Prawal.Sharma@dentsu.com work or school account.\nClick here for troubleshooting information"


class ReliabilityTests(unittest.TestCase):
    def test_item_count_counts_all_supported_items(self):
        data = {
            "emails": [{"subject": "a"}],
            "chats": [{"from": "b"}],
            "messages": [{"chat": "c"}],
            "events": [{"title": "d"}],
        }
        self.assertEqual(reliability.item_count(data), 4)

    def test_delta_handles_boolean_and_string_unread_counts(self):
        fresh = {"messages": [{"chat": "A", "unread": True}, {"channel": "B", "unread": "3"}]}
        prev = {"messages": [{"chat": "A", "unread": False}, {"channel": "B", "unread": "1"}]}
        delta = _delta(fresh, prev)
        self.assertEqual(
            sorted(delta, key=lambda item: item["channel"]),
            [
                {"kind": "message", "channel": "A", "count": 1},
                {"kind": "message", "channel": "B", "count": 2},
            ],
        )

    def test_failure_meta_classifies_auth_and_records_duration(self):
        started = time.perf_counter()
        result = reliability.attach_failure_meta(
            None,
            "teams",
            "teams",
            started,
            "not_logged_in",
        )
        self.assertEqual(result["meta"]["status"], "error")
        self.assertEqual(result["meta"]["error_code"], "auth")
        self.assertEqual(result["meta"]["auth_status"], "needs_login")
        self.assertGreaterEqual(result["meta"]["duration_ms"], 0)

    def test_outlook_calendar_popup_24h_time_is_normalized(self):
        parsed = _parse_outlook_calendar_time(
            "Co-op x dentsu x Premier Foods Pride Month Event\nThu 18-06-2026 16:30 - 17:30\nMS Teams"
        )
        self.assertEqual(parsed["time"], "4:30pm to 5:30pm")
        self.assertIsInstance(parsed["start_ts"], int)
        self.assertIsInstance(parsed["end_ts"], int)


class AuthWallTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_wall_detects_teams_aria_signin_shell(self):
        self.assertTrue(await _is_auth_wall(FakeAuthShellPage()))


if __name__ == "__main__":
    unittest.main()
