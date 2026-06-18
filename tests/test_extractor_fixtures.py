import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_HOME = Path(os.environ.get("HOME", str(Path.home())))
TEST_HOME = Path("/private/tmp/jarvis-test-home")
TEST_HOME.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(TEST_HOME)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(REAL_HOME / "Library" / "Caches" / "ms-playwright"))
os.environ.setdefault("GROQ_API_KEY", "test-key")
sys.path.insert(0, str(ROOT / "backend"))

from extractor_scripts import (
    GOOGLE_CHAT_UNREAD_JS,
    OUTLOOK_CALENDAR_BODY_TEXT_JS,
    OUTLOOK_CALENDAR_FALLBACK_JS,
    OUTLOOK_MAIL_UNREAD_JS,
    TEAMS_CHAT_FALLBACK_JS,
)
from browser_agent import extract_gmail_unread_emails, extract_slack_unread_channels

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - import failure is handled by skip
    async_playwright = None


FIXTURES = ROOT / "tests" / "fixtures"


@unittest.skipIf(async_playwright is None, "playwright is not installed")
class ExtractorFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        try:
            self.browser = await self.pw.chromium.launch(headless=True)
        except Exception as exc:
            await self.pw.stop()
            raise unittest.SkipTest(f"Playwright Chromium could not launch in this environment: {exc}")
        self.page = await self.browser.new_page(viewport={"width": 1280, "height": 800})

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def load_fixture(self, name: str):
        await self.page.set_content((FIXTURES / name).read_text(), wait_until="domcontentloaded")

    async def test_gmail_unread_and_empty_fixtures(self):
        await self.load_fixture("gmail_mail_unread.html")
        unread = await extract_gmail_unread_emails(self.page)
        self.assertEqual(unread, [{
            "sender": "Sam Rao",
            "subject": "Budget approval needed",
            "snippet": "Can you approve the Q3 budget today?",
        }])

        await self.load_fixture("gmail_mail_empty.html")
        self.assertEqual(await extract_gmail_unread_emails(self.page), [])

    async def test_google_chat_unread_and_empty_fixtures(self):
        await self.load_fixture("google_chat_unread.html")
        chats = await self.page.evaluate(GOOGLE_CHAT_UNREAD_JS)
        self.assertEqual(chats, [
            {"from": "Project Phoenix", "preview": "Priya posted a blocker in the release thread", "type": "space"},
            {"from": "Mira Shah", "preview": "Please review the launch notes", "type": "dm"},
        ])

        await self.load_fixture("google_chat_empty.html")
        self.assertEqual(await self.page.evaluate(GOOGLE_CHAT_UNREAD_JS), [])

    async def test_slack_unread_and_empty_fixtures(self):
        await self.load_fixture("slack_unread.html")
        channels = await extract_slack_unread_channels(self.page)
        simplified = [{k: ch[k] for k in ["channel", "unread", "is_mention"]} for ch in channels]
        self.assertEqual(simplified, [
            {"channel": "proj-alpha", "unread": "4", "is_mention": False},
            {"channel": "direct-sam", "unread": "1", "is_mention": True},
        ])

        await self.load_fixture("slack_empty.html")
        self.assertEqual(await extract_slack_unread_channels(self.page), [])

    async def test_outlook_mail_unread_and_empty_fixtures(self):
        await self.load_fixture("outlook_mail_unread.html")
        emails = await self.page.evaluate(OUTLOOK_MAIL_UNREAD_JS)
        self.assertEqual(emails, [{
            "sender": "Riya Mehta",
            "subject": "Contract review",
            "snippet": "Please review the redlines before 3 PM",
        }])

        await self.load_fixture("outlook_mail_empty.html")
        self.assertEqual(await self.page.evaluate(OUTLOOK_MAIL_UNREAD_JS), [])

    async def test_teams_unread_and_empty_fixtures(self):
        await self.load_fixture("teams_unread.html")
        chats = await self.page.evaluate(TEAMS_CHAT_FALLBACK_JS)
        self.assertEqual(sorted(chats, key=lambda item: item["chat"]), [
            {"chat": "Arjun Nair", "preview": "Need your confirmation on the rollout 2"},
            {"chat": "Operations", "preview": "Release train update 3"},
        ])

        await self.load_fixture("teams_empty.html")
        self.assertEqual(await self.page.evaluate(TEAMS_CHAT_FALLBACK_JS), [])

    async def test_outlook_calendar_unread_and_empty_fixtures(self):
        await self.load_fixture("outlook_calendar_unread.html")
        events = await self.page.evaluate(OUTLOOK_CALENDAR_FALLBACK_JS)
        self.assertEqual(events, [{
            "title": "Design review, 2:30 PM to 3:00 PM, accepted, organized by Maya Singh",
            "time": "2:30 PM",
        }])

        await self.load_fixture("outlook_calendar_empty.html")
        self.assertEqual(await self.page.evaluate(OUTLOOK_CALENDAR_FALLBACK_JS), [])

    async def test_outlook_calendar_body_text_fallback_fixture(self):
        await self.load_fixture("outlook_calendar_body_text.html")
        titles = await self.page.evaluate(OUTLOOK_CALENDAR_BODY_TEXT_JS)
        self.assertEqual(titles, [
            "DR Internal Standup",
            "Co-op x dentsu x Premier Foods Pride Month Event (Proud Hero Event)",
            "Data Refinery BAU : DSM",
        ])


if __name__ == "__main__":
    unittest.main()
