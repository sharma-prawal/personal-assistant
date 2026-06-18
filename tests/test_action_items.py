import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_HOME = Path("/private/tmp/jarvis-test-home")
TEST_HOME.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(TEST_HOME)
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("GROQ_API_KEY", "test-key")

from assistant import _extract_json_array, _validate_action_items


class ActionItemValidationTests(unittest.TestCase):
    def test_extracts_json_array_from_wrapped_text(self):
        parsed = _extract_json_array('Here: [{"priority":"critical","item":"Reply","detail":"Reply to Sam.","source":"Gmail"}]')
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed[0]["item"], "Reply")

    def test_extracts_empty_json_array_as_valid_data(self):
        self.assertEqual(_extract_json_array("[]"), [])

    def test_validation_drops_incomplete_items_and_normalizes_priority(self):
        items = _validate_action_items([
            {"priority": "urgent", "item": "Reply to Sam", "detail": "Sam asked for a status update.", "source": "Gmail"},
            {"priority": "critical", "item": "", "detail": "Missing item"},
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["priority"], "info")
        self.assertEqual(items[0]["source"], "Gmail")

    def test_validation_drops_scraper_health_items(self):
        items = _validate_action_items([
            {
                "priority": "critical",
                "item": "Fix Dentsu Calendar error",
                "source": "Dentsu Calendar",
                "from": "",
                "subject": "Error during scan",
                "detail": "The system encountered an error while scanning Dentsu Calendar: Invalid or unexpected token.",
            },
            {
                "priority": "important",
                "item": "Reply to Mira on launch notes",
                "source": "Slack",
                "from": "Mira Shah",
                "subject": "Launch notes",
                "detail": "Mira asked for your review of the launch notes before today's review.",
            },
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["from"], "Mira Shah")

    def test_validation_drops_routine_calendar_items(self):
        items = _validate_action_items([
            {
                "priority": "important",
                "item": "Join Spire Internal PHP Standup",
                "source": "Successive Calendar",
                "from": "",
                "subject": "Spire Internal PHP Standup",
                "detail": "Your 4:30pm meeting starts in a few hours.",
            },
            {
                "priority": "info",
                "item": "Review Whiskey Stand Up",
                "source": "Successive Calendar",
                "from": "",
                "subject": "Whiskey Stand Up",
                "detail": "This is a scheduled stand up tonight.",
            },
            {
                "priority": "important",
                "item": "Reply to Mira on launch notes",
                "source": "Slack",
                "from": "Mira Shah",
                "subject": "Launch notes",
                "detail": "Mira asked for your review of the launch notes before today's review.",
            },
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "Slack")


if __name__ == "__main__":
    unittest.main()
