import asyncio
import datetime as _dt_global
import json
import os
import re as _re_global
import time
from pathlib import Path
from typing import AsyncIterator, Callable
from playwright.async_api import async_playwright, BrowserContext, Page

from extractor_scripts import (
    GOOGLE_CHAT_UNREAD_JS,
    OUTLOOK_CALENDAR_BODY_TEXT_JS,
    OUTLOOK_CALENDAR_FALLBACK_JS,
    OUTLOOK_MAIL_UNREAD_JS,
    TEAMS_CHAT_FALLBACK_JS,
)
import reliability

SESSION_DIR = Path.home() / ".jarvis" / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)
try:
    SESSION_DIR.chmod(0o700)  # Only owner can read — auth tokens stored here
except PermissionError:
    print(f"[sessions] warning: could not chmod {SESSION_DIR}; continuing with existing permissions", flush=True)

GMAIL_1_EMAIL = os.getenv("GMAIL_1_EMAIL", "Account 1")
GMAIL_2_EMAIL = os.getenv("GMAIL_2_EMAIL", "Account 2")
OUTLOOK_EMAIL = os.getenv("OUTLOOK_EMAIL", "Outlook")
# office365.com is where corporate Okta (myapps) SSO lands — cloud.microsoft triggers
# a Microsoft step-up auth password prompt that the office365 domain does not
OUTLOOK_URL   = os.getenv("OUTLOOK_URL", "https://outlook.office365.com").rstrip("/")

# Optional corporate Okta portal (e.g. https://myapps.yourcompany.com/). When set it is the
# sign-in entry point for Outlook + Teams; when empty JARVIS goes to Outlook and Teams directly.
MYAPPS_URL = os.getenv("MYAPPS_URL", "").strip()


def _default_label(email: str, fallback: str) -> str:
    if "@" in email:
        return email.split("@")[1].split(".")[0].capitalize()
    return fallback


GMAIL_1_LABEL = os.getenv("GMAIL_1_LABEL", _default_label(GMAIL_1_EMAIL, "Gmail 1"))
GMAIL_2_LABEL = os.getenv("GMAIL_2_LABEL", _default_label(GMAIL_2_EMAIL, "Gmail 2"))
OUTLOOK_LABEL = os.getenv("OUTLOOK_LABEL", "Outlook")


def _format_12h(hour: int, minute: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d}{suffix}"


def _parse_outlook_calendar_time(text: str) -> dict:
    """Extract a calendar time range from Outlook popup text."""
    text = text or ""

    # New Outlook popup format: "Thu 18-06-2026 16:30 - 17:30"
    dated_24h = _re_global.search(
        r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\s+(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\b",
        text,
    )
    if dated_24h:
        day, month, year, sh, sm, eh, em = map(int, dated_24h.groups())
        try:
            start_dt = _dt_global.datetime(year, month, day, sh, sm)
            end_dt = _dt_global.datetime(year, month, day, eh, em)
            now_ms = int(time.time() * 1000)
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms = int(end_dt.timestamp() * 1000)
            return {
                "time": f"{_format_12h(sh, sm)} to {_format_12h(eh, em)}",
                "start_ts": start_ms // 1000,
                "end_ts": end_ms // 1000,
                "status": "concluded" if end_ms < now_ms else ("ongoing" if start_ms <= now_ms <= end_ms else "upcoming"),
            }
        except ValueError:
            pass

    ampm_range = _re_global.search(
        r"(\d{1,2}(?::\d{2})?\s*[AP]M)\s*[-–—to]+\s*(\d{1,2}(?::\d{2})?\s*[AP]M)",
        text,
        _re_global.IGNORECASE,
    )
    if ampm_range:
        return {"time": f"{ampm_range.group(1).upper()} to {ampm_range.group(2).upper()}"}

    simple = _re_global.search(r"(\d{1,2}(?::\d{2})?\s*[AP]M)", text, _re_global.IGNORECASE)
    if simple:
        return {"time": simple.group(1).upper()}
    return {}


async def _get_context(playwright, profile_name: str, headless: bool) -> BrowserContext:
    session_path = str(SESSION_DIR / profile_name)
    return await playwright.chromium.launch_persistent_context(
        session_path,
        headless=headless,
        viewport={"width": 1280, "height": 800},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _is_logged_in_session(session_path: Path) -> bool:
    return (session_path / "Default" / "Cookies").exists()


# Known Microsoft/Google login domains beyond the obvious ones
_AUTH_WALL_DOMAINS = [
    "login.live.com", "login.microsoftonline.com", "login.microsoft.com",
    "login.windows.net", "sts.", "/adfs/", "saml.", "/saml2",
    "accounts.google.com", "okta.com", "onelogin.com", "pingidentity.com",
    "/login", "/signin",
    "myapps.",        # Corporate Okta portals (myapps.company.com)
    "sso/wsfed",      # WS-Federation SSO endpoints
    "wsfed/passive",  # WS-Federation passive flow
]
_AUTH_BODY_SIGNALS = [
    "sign in to your account", "sign in with microsoft", "sign in with google",
    "enter your password", "enter the password", "we couldn't sign you in",
    "your account or password is incorrect", "session has expired",
    "you've been signed out", "pick an account", "sign into outlook",
    "to protect your account", "verify your identity",
    "work or school account", "click here for troubleshooting information",
    "troubleshooting information",
    # New Outlook (cloud.microsoft) SPA renders this inline without changing the URL
    "verify with your password", "back to sign in",
]


async def _is_auth_wall(page) -> bool:
    """Return True if the page looks like a login/auth prompt (any SSO provider)."""
    url = page.url.lower()
    if any(d in url for d in _AUTH_WALL_DOMAINS):
        return True
    try:
        title = (await page.title()).lower()
        if any(t in title for t in ["sign in", "log in", "login", "pick an account", "authenticate"]):
            return True
    except Exception:
        pass
    try:
        body = (await page.inner_text("body"))[:2500].lower()
        if any(s in body for s in _AUTH_BODY_SIGNALS):
            return True
    except Exception:
        pass
    try:
        aria = await page.evaluate("""() => [...document.querySelectorAll('[aria-label]')]
            .map(e => e.getAttribute('aria-label') || '')
            .filter(Boolean)
            .join('\\n')
            .slice(0, 2500)
            .toLowerCase()
        """)
        if any(s in aria for s in _AUTH_BODY_SIGNALS):
            return True
    except Exception:
        pass
    return False


async def _launch_via_myapps(
    context: BrowserContext,
    tile_label: str,
    target_url_check,   # callable (url: str) -> bool
):
    """
    Open the corporate Okta portal, click the named app tile, and return the page
    once it lands on a URL accepted by target_url_check.
    Returns None if no portal is configured, the Okta session has expired or the tile
    cannot be found.
    """
    if not MYAPPS_URL:
        return None
    portal_page = await context.new_page()
    new_page = None
    try:
        print(f"[myapps] opening portal for '{tile_label}'", flush=True)
        await portal_page.goto(MYAPPS_URL, wait_until="domcontentloaded", timeout=30000)

        # Okta's OAuth2/OIDC redirect chain can take several seconds after domcontentloaded.
        # Wait until we land on either the app dashboard (UserHome) or an interactive login page.
        try:
            await portal_page.wait_for_url(
                lambda url: (
                    "UserHome" in url or
                    "session_hint=AUTHENTICATED" in url or
                    "/login" in url or
                    "/signin" in url or
                    "login.microsoftonline.com" in url
                ),
                timeout=20000,
            )
        except Exception:
            pass  # If still on oauth2/authorize after 20s, fall through to the check below

        await portal_page.wait_for_timeout(1000)
        url_now = portal_page.url
        print(f"[myapps] portal settled: {url_now[:100]}", flush=True)

        is_dashboard = "UserHome" in url_now or "session_hint=AUTHENTICATED" in url_now
        if not is_dashboard:
            print(f"[myapps] not on dashboard (still at auth step) — need Setup Logins re-auth", flush=True)
            await portal_page.close()
            return None

        # Wait for the app tile grid to appear.
        # The body session-expired check is done AFTER this wait so we catch a genuine
        # expiry modal vs a transient one shown during the OAuth2 callback.
        _TILE_GRID_SELECTORS = (
            '[data-se="app-logo"], [class*="app-logo"], [class*="appTile"], [class*="AppTile"], '
            '[class*="app-button"], [class*="appButton"], .o-app-list a, '
            '[class*="app-list"] a, [class*="appList"] a, '
            'a[href*=".okta.com"], a[href*="myapps"]'
        )
        try:
            await portal_page.wait_for_selector(_TILE_GRID_SELECTORS, timeout=15000)
            print(f"[myapps] app grid loaded", flush=True)
        except Exception:
            # Grid didn't appear — may be session expired or SPA still loading
            await portal_page.wait_for_timeout(3000)
            print(f"[myapps] app grid selector timed out, checking body", flush=True)

        # Check for session-expired modal only after the SPA has had time to settle.
        # Checking too early can catch the brief "loading" state of the OAuth2 callback.
        try:
            body = (await portal_page.inner_text("body"))[:1500].lower()
            if "session has expired" in body or "log in again" in body or "sign in again" in body:
                print(f"[myapps] session-expired modal detected — need re-login", flush=True)
                await portal_page.close()
                return None
        except Exception:
            pass

        # Also wait for network to settle to avoid execution-context-destroyed errors
        try:
            await portal_page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        # Locate the tile with up to 3 retries in case the SPA re-renders mid-query
        tile = None
        for _tile_attempt in range(3):
            try:
                tile = await portal_page.query_selector(f'[title="{tile_label}"]')
                if not tile:
                    tile = await portal_page.query_selector(
                        f'[aria-label="{tile_label}"], [data-se="{tile_label}"]'
                    )
                if not tile:
                    # Broader scan — Okta tiles use div/li/a with text content (no title attr)
                    for el in await portal_page.query_selector_all(
                        '[data-se="app-logo"], [class*="app-logo"], [class*="appTile"], '
                        '[class*="app-button"], .o-app-list a, a[href], '
                        '[role="link"], [role="button"], li, div'
                    ):
                        try:
                            text = (await el.inner_text() or "").strip()
                            if tile_label.lower() in text.lower() and len(text) < 100:
                                tile = el
                                break
                        except Exception:
                            continue
                if not tile:
                    tile = await portal_page.query_selector(f'text="{tile_label}"')
                break  # Succeeded (tile found or definitively absent)
            except Exception as e:
                if _tile_attempt < 2:
                    print(f"[myapps] tile query retry {_tile_attempt + 1} after: {e}", flush=True)
                    await portal_page.wait_for_timeout(2000)
                    continue
                print(f"[myapps] tile query failed after 3 attempts: {e}", flush=True)

        if not tile:
            # Log diagnostic to identify exact tile names and page state
            try:
                _diag = await portal_page.evaluate("""() => ({
                    titles:    [...document.querySelectorAll('[title]')].map(e => e.getAttribute('title')).filter(Boolean).slice(0, 20),
                    ariaLabels:[...document.querySelectorAll('[aria-label]')].map(e => e.getAttribute('aria-label')).filter(Boolean).slice(0, 20),
                    allText:   document.body.innerText.substring(0, 600),
                    linkCount: document.querySelectorAll('a[href]').length,
                    btnCount:  document.querySelectorAll('button,[role="button"]').length,
                })""")
                print(f"[myapps] tile '{tile_label}' not found — diag: {_diag}", flush=True)
            except Exception:
                print(f"[myapps] tile '{tile_label}' not found — check the app name on your Okta portal", flush=True)
            await portal_page.close()
            return None

        print(f"[myapps] clicking tile '{tile_label}'", flush=True)

        # Okta app tiles normally open in a new tab (_blank); capture it
        try:
            async with context.expect_page(timeout=8000) as page_event:
                await tile.click()
            new_page = await page_event.value
            await portal_page.close()
            portal_page = None
        except Exception:
            # Opened in the same tab (redirect, not _blank)
            new_page = portal_page
            portal_page = None

        # Wait for the final destination — may pass through a WS-Fed handoff
        await new_page.wait_for_url(target_url_check, timeout=60000)
        print(f"[myapps] ✓ landed on target: {new_page.url[:80]}", flush=True)
        return new_page

    except Exception as e:
        print(f"[myapps] '{tile_label}' launch failed: {e}", flush=True)
        for p in [new_page, portal_page]:
            if p:
                try:
                    await p.close()
                except Exception:
                    pass
        return None


async def scrape_gmail(context: BrowserContext, email: str, label: str) -> dict:
    page = await context.new_page()
    result = {"service": label, "emails": [], "chats": [], "error": None}
    # ?authuser=email routes Google Workspace to the correct account
    _mail_url = f"https://mail.google.com/mail/?authuser={email}#inbox"
    _chat_url  = f"https://chat.google.com/?authuser={email}"
    try:
        await page.goto(_mail_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        if await _is_auth_wall(page):
            result["error"] = "not_logged_in"
            result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, f"{label}_gmail", result["error"])
            return result

        result["emails"] = await extract_gmail_unread_emails(page)

        # Switch to Google Chat (account-specific URL)
        await page.goto(_chat_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)

        # Unread items: Google Chat marks unread sidebar entries with "Unread" in innerText.
        # data-dm-id is gone; .b7NJPb badge class is gone. Use text-based detection instead.
        chat_data = await page.evaluate(GOOGLE_CHAT_UNREAD_JS)
        print(f"[google-chat] {label} found {len(chat_data)} unread chat(s)", flush=True)
        result["chats"] = chat_data[:10]

    except Exception as e:
        result["error"] = str(e)
        result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, f"{label}_gmail", e)
    finally:
        await page.close()
    return result


async def extract_gmail_unread_emails(page: Page) -> list[dict]:
    emails = []
    email_rows = await page.query_selector_all('tr.zA.zE')
    for row in email_rows[:10]:
        try:
            sender_el = await row.query_selector('.yX.xY span[email]')
            subject_el = await row.query_selector('.y6 span')
            snippet_el = await row.query_selector('.y2')

            sender_name  = (await sender_el.get_attribute("name")  or "") if sender_el else ""
            sender_title = (await sender_el.get_attribute("title") or "") if sender_el else ""
            sender_email = (await sender_el.get_attribute("email") or "") if sender_el else ""
            sender = sender_title or sender_name or sender_email or "Unknown"
            subject = await subject_el.inner_text() if subject_el else "(no subject)"
            snippet = await snippet_el.inner_text() if snippet_el else ""
            emails.append({"sender": sender, "subject": subject.strip(), "snippet": snippet.strip()})
        except Exception:
            continue
    return emails


async def scrape_outlook(context: BrowserContext) -> dict:
    result = {"service": f"{OUTLOOK_LABEL} Outlook", "emails": [], "error": None}

    # Selectors for email rows — covers classic OWA, new OWA (cloud.microsoft), and new Outlook
    _OWA_ROW_SEL = (
        '[role="option"][aria-label], '
        '[data-automation-id="MailListItem"], '
        '[role="listitem"][aria-label], '
        '[data-convid], '
        '[class*="MailListItem"], [class*="mail-list-item"], [class*="messageListItem"]'
    )
    _OUTLOOK_READY = lambda u: any(d in u for d in [
        "outlook.cloud.microsoft", "outlook.office.com", "outlook.office365.com"
    ])

    # Strategy: try the direct Microsoft URL first using saved session cookies.
    # office365.com does NOT trigger the step-up password prompt that cloud.microsoft does.
    # Only fall back to myapps (Okta WS-Fed) when the MS session itself has expired.
    # This avoids hitting the Okta SPA on every briefing (Okta uses sessionStorage which
    # is cleared on browser restart, causing "Your session has expired" even with valid cookies).
    page = await context.new_page()
    try:
        await page.goto(f"{OUTLOOK_URL}/mail/inbox", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(3000)  # New Outlook SPA needs ~3s to render its auth wall
        _direct_ok = _OUTLOOK_READY(page.url) and not await _is_auth_wall(page)
    except Exception as e:
        print(f"[outlook-mail] direct URL attempt failed: {e}", flush=True)
        _direct_ok = False

    if not _direct_ok:
        print(f"[outlook-mail] direct URL auth wall ({page.url[:80]}) — using myapps tile", flush=True)
        try:
            await page.close()
        except Exception:
            pass
        page = await _launch_via_myapps(context, "O365 - Mail", _OUTLOOK_READY)
        if page is None:
            result["error"] = "not_logged_in"
            return result
        # Stay on the domain myapps landed on — do NOT cross domains
        if _OUTLOOK_READY(page.url) and "mail/inbox" not in page.url.lower():
            from urllib.parse import urlparse as _urlparse
            _lp = _urlparse(page.url)
            await page.goto(f"{_lp.scheme}://{_lp.netloc}/mail/inbox", wait_until="domcontentloaded", timeout=20000)
    else:
        print(f"[outlook-mail] direct URL succeeded: {page.url[:80]}", flush=True)

    try:
        print(f"[outlook-mail] final URL: {page.url}", flush=True)

        # SPA settle time
        await page.wait_for_timeout(5000)

        # Check for inline SPA auth prompts (new Outlook shows these without URL change)
        if await _is_auth_wall(page):
            print(f"[outlook-mail] SPA auth wall after settle — need re-login", flush=True)
            result["error"] = "not_logged_in"
            result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "outlook", result["error"])
            return result

        # Diagnostic: log what selectors are visible on the page
        diag = await page.evaluate("""() => ({
            roleOptions:  document.querySelectorAll('[role="option"]').length,
            dataConvid:   document.querySelectorAll('[data-convid]').length,
            mailListItem: document.querySelectorAll('[data-automation-id="MailListItem"]').length,
            classMailList:document.querySelectorAll('[class*="MailListItem"]').length,
            bodySnippet:  document.body.innerText.substring(0, 300),
        })""")
        print(f"[outlook-mail] diag: role=option:{diag['roleOptions']} convid:{diag['dataConvid']} "
              f"MailListItem:{diag['mailListItem']} class:{diag['classMailList']}", flush=True)
        print(f"[outlook-mail] body preview: {diag['bodySnippet'][:200]}", flush=True)

        # Use JavaScript to extract email data directly — more reliable than CSS selectors
        # against new Outlook's obfuscated class names and dynamic DOM structure.
        emails_js = await page.evaluate(OUTLOOK_MAIL_UNREAD_JS)
        print(f"[outlook-mail] JS found {len(emails_js)} unread email(s)", flush=True)
        for em in emails_js:
            result["emails"].append({
                "sender":  em.get("sender") or "Unknown",
                "subject": em.get("subject") or "(no subject)",
                "snippet": em.get("snippet", ""),
            })

    except Exception as e:
        result["error"] = str(e)
        result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "outlook", e)
    finally:
        await page.close()
    return result


async def scrape_slack(context: BrowserContext) -> dict:
    page = await context.new_page()
    result = {"service": "Slack", "messages": [], "error": None}
    try:
        await page.goto("https://app.slack.com/client", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(6000)

        if "slack.com/signin" in page.url or "slack.com/get-started" in page.url:
            result["error"] = "not_logged_in"
            result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "slack", result["error"])
            return result

        # Unread items in sidebar — collect metadata first
        channels_to_read = await extract_slack_unread_channels(page)

        # Click into each channel to read actual messages
        for ch in channels_to_read[:6]:
            try:
                await ch["el"].click()
                await page.wait_for_timeout(2500)

                # Grab recent messages from the message pane
                recent_msgs = []
                msg_containers = await page.query_selector_all(
                    '[data-qa="message_container"], [data-qa="virtual-list-item"]'
                )
                for msg_el in msg_containers[-8:]:
                    try:
                        sender_el = await msg_el.query_selector(
                            '[data-qa="message_sender_name"], .c-message__sender_link, [class*="sender"]'
                        )
                        text_el = await msg_el.query_selector(
                            '[data-qa="message-text"], .c-message_kit__text, [class*="message_body"]'
                        )
                        sender = (await sender_el.inner_text()).strip() if sender_el else ""
                        text   = (await text_el.inner_text()).strip() if text_el else ""
                        text   = " ".join(text.split())[:200]  # collapse whitespace
                        if sender or text:
                            recent_msgs.append({"sender": sender, "text": text})
                    except Exception:
                        continue

                result["messages"].append({
                    "channel":         ch["channel"],
                    "unread":          ch["unread"],
                    "is_mention":      ch["is_mention"],
                    "recent_messages": recent_msgs,
                })
            except Exception:
                # If click/read fails, still record the channel with count
                result["messages"].append({
                    "channel":         ch["channel"],
                    "unread":          ch["unread"],
                    "is_mention":      ch["is_mention"],
                    "recent_messages": [],
                })

    except Exception as e:
        result["error"] = str(e)
        result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "slack", e)
    finally:
        await page.close()
    return result


async def extract_slack_unread_channels(page: Page) -> list[dict]:
    unread_items = await page.query_selector_all('.p-channel_sidebar__channel--unread')
    channels_to_read = []
    for item in unread_items[:8]:
        try:
            name_el = await item.query_selector('.p-channel_sidebar__name')
            name = (await name_el.inner_text()).strip() if name_el else ""
            if not name:
                continue
            mention_badge = await item.query_selector('.c-mention_badge')
            unread_badge  = await item.query_selector('.p-channel_sidebar__badge')
            badge_el = mention_badge or unread_badge
            count = (await badge_el.inner_text()).strip() if badge_el else "?"
            channels_to_read.append({
                "el": item,
                "channel": name,
                "unread": count,
                "is_mention": mention_badge is not None,
            })
        except Exception:
            continue
    return channels_to_read


async def scrape_teams(context: BrowserContext) -> dict:
    result = {"service": "Teams", "messages": [], "error": None}

    # Selectors that only appear once Teams has fully loaded (past the splash screen)
    _TEAMS_READY_SELECTORS = (
        '[data-tid*="chat-list-item"], .fui-ChatListItem, '
        '[aria-label="Chat list"], [id="chat-list"], '
        '[data-testid="chat-list"], [class*="chatList"]'
    )
    _TEAMS_READY = lambda u: "teams.microsoft.com" in u and "signout" not in u

    # Try direct Teams URL first — avoids the Okta sessionStorage issue on browser restart.
    # Fall back to myapps tile only when the MS session has actually expired.
    page = await context.new_page()
    try:
        await page.goto("https://teams.microsoft.com/v2/#/conversations", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2000)
        _direct_ok = _TEAMS_READY(page.url) and not await _is_auth_wall(page)
    except Exception as e:
        print(f"[teams] direct URL attempt failed: {e}", flush=True)
        _direct_ok = False

    if not _direct_ok:
        print(f"[teams] direct URL auth wall ({page.url[:80]}) — trying myapps tile", flush=True)
        try:
            await page.close()
        except Exception:
            pass
        page = await _launch_via_myapps(context, "O365 - Teams", _TEAMS_READY)
        if page is None:
            result["error"] = "not_logged_in"
            return result
    else:
        print(f"[teams] direct URL succeeded: {page.url[:80]}", flush=True)

    try:
        # New Teams v2 ignores the #/conversations fragment — it always settles on v2/.
        # The "hamburger-button" data-tid appears when Teams' main layout has initialised
        # (title bar + left rail ready). Wait for it before attempting any nav clicks.
        try:
            await page.wait_for_selector('[data-tid="hamburger-button"]', timeout=45000)
            print(f"[teams] main layout ready (hamburger visible)", flush=True)
        except Exception:
            print(f"[teams] hamburger wait timed out — proceeding", flush=True)

        # Give the left-rail app icons a moment to render after the layout is ready
        await page.wait_for_timeout(3000)

        # Click the "Chat" / "Chats" nav item so the chat list is the active section.
        # New Teams v2 uses "Chats" (plural) and data-tid="app-bar-icon-Chats".
        _chat_clicked = False
        # Teams v2 uses aria-label="Chat (⌃ ⇧ 2)" — use ^= starts-with matching
        _chat_sels = [
            '[data-tid="app-bar-icon-Chats"]',
            '[data-tid="app-bar-icon-Chat"]',
            '[data-tid="left-rail-chat-button"]',
            '[data-tid="leftRail-chat"]',
            '[aria-label^="Chat "]',   # matches "Chat (⌃ ⇧ 2)"
            '[aria-label^="Chats "]',  # matches "Chats (⌃ ⇧ 2)" if present
            'nav [aria-label^="Chat"]',
            '[aria-label="Chats"]',
            '[aria-label="Chat"]',
            '[title^="Chat"]',
        ]
        for _chat_sel in _chat_sels:
            _chat_btn = await page.query_selector(_chat_sel)
            if _chat_btn:
                try:
                    await _chat_btn.click()
                    _chat_clicked = True
                    print(f"[teams] clicked Chat nav button via '{_chat_sel}'", flush=True)
                    break
                except Exception:
                    pass
        if not _chat_clicked:
            # Last resort: JS scan of all nav elements for "chat" or "chats" text
            try:
                _chat_js = await page.evaluate("""() => {
                    // Teams v2 uses aria-label="Chat (⌃ ⇧ 2)" — scan all elements, not just nav
                    const allEls = [...document.querySelectorAll('[aria-label]')];
                    for (const el of allEls) {
                        const label = (el.getAttribute('aria-label') || '').toLowerCase();
                        if (label === 'chat' || label === 'chats' ||
                            label.startsWith('chat ') || label.startsWith('chats ')) {
                            el.click(); return label;
                        }
                    }
                    return null;
                }""")
                if _chat_js:
                    _chat_clicked = True
                    print(f"[teams] clicked Chat via JS scan (aria-label='{_chat_js}')", flush=True)
            except Exception:
                pass
        if not _chat_clicked:
            # Log what nav elements exist to help diagnose future selector mismatches
            try:
                _nav_labels = await page.evaluate(
                    "() => [...document.querySelectorAll('[aria-label]')].map(e => e.getAttribute('aria-label')).filter(Boolean).slice(0, 30)"
                )
                print(f"[teams] Chat nav not found. aria-labels: {_nav_labels}", flush=True)
            except Exception:
                print("[teams] Chat nav button not found — proceeding as-is", flush=True)

        # Wait for the chat list to render after clicking Chat.
        # Teams takes a few seconds to switch sections and load DM items.
        await page.wait_for_timeout(5000)
        try:
            await page.wait_for_selector(_TEAMS_READY_SELECTORS, timeout=20000)
        except Exception:
            pass  # JS fallback below will still attempt extraction

        await page.wait_for_timeout(3000)

        print(f"[teams] final URL: {page.url}", flush=True)

        if await _is_auth_wall(page):
            print(f"[teams] auth wall at: {page.url[:80]}", flush=True)
            result["error"] = "not_logged_in"
            result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "teams", result["error"])
            return result

        # Diagnostic: count elements and show body snippet to identify correct selectors
        diag = await page.evaluate("""() => ({
            chatItems:    document.querySelectorAll('[data-tid*="chat-list-item"]').length,
            fuiItems:     document.querySelectorAll('.fui-ChatListItem').length,
            listItems:    document.querySelectorAll('[role="listitem"]').length,
            options:      document.querySelectorAll('[role="option"]').length,
            ariaLabels:   document.querySelectorAll('[aria-label]').length,
            dataTestid:   document.querySelectorAll('[data-testid]').length,
            dataTid:      document.querySelectorAll('[data-tid]').length,
            bodyLen:      document.body.innerText.length,
            bodySnippet:  document.body.innerText.substring(0, 600),
            unreadAriaLabels: [...document.querySelectorAll('[aria-label]')]
                .map(e => e.getAttribute('aria-label'))
                .filter(l => l && l.toLowerCase().includes('unread'))
                .slice(0, 10),
            dataTidSample: [...document.querySelectorAll('[data-tid]')]
                .map(e => e.getAttribute('data-tid'))
                .filter(Boolean)
                .slice(0, 20),
        })""")
        print(f"[teams] diag: chatItems={diag['chatItems']} fuiItems={diag['fuiItems']} "
              f"listItems={diag['listItems']} dataTid={diag['dataTid']} dataTestid={diag['dataTestid']}", flush=True)
        print(f"[teams] unread aria-labels: {diag['unreadAriaLabels']}", flush=True)
        print(f"[teams] data-tid sample: {diag['dataTidSample']}", flush=True)
        print(f"[teams] body snippet: {diag['bodySnippet'][:300]}", flush=True)

        # Teams unread detection — try multiple selector patterns across old/new Teams UI
        unread_els = []
        UNREAD_SELECTORS = [
            # Old Teams (data-tid based)
            '[data-tid*="chat-list-item"][aria-label*="unread"]',
            '[data-tid*="chat-list-item"][aria-label*="Unread"]',
            '[data-tid*="chat-list-item"][aria-label*="new message"]',
            # New Teams (Fluent UI)
            '.fui-ChatListItem[aria-label*="unread"]',
            '.fui-ChatListItem[aria-label*="Unread"]',
        ]
        for sel in UNREAD_SELECTORS:
            found = await page.query_selector_all(sel)
            if found:
                unread_els = found
                break

        # Final fallback: all chat list items that have a visible badge/count
        if not unread_els:
            all_items = await page.query_selector_all(
                '[data-tid*="chat-list-item"], .fui-ChatListItem'
            )
            for item in all_items:
                badge = await item.query_selector(
                    '[data-tid*="badge"], [class*="unreadBadge"], [class*="unread-badge"], '
                    '[class*="notificationBadge"], [aria-label*="unread"]'
                )
                if badge:
                    unread_els.append(item)

        print(f"[teams] CSS selectors found {len(unread_els)} unread item(s)", flush=True)

        # JS-based extraction — Teams v2 uses obfuscated/dynamic class names.
        # Strategy: find any element with a numeric unread badge child, or aria-label="unread",
        # then walk up to the containing chat-row element for the sender name + preview.
        if not unread_els:
            js_chats = await page.evaluate(TEAMS_CHAT_FALLBACK_JS)
            print(f"[teams] JS fallback found {len(js_chats)} chat(s)", flush=True)
            for ch in js_chats:
                result["messages"].append({
                    "chat":            ch.get("chat", "Unknown"),
                    "unread":          True,
                    "is_dm":           False,
                    "is_mention":      False,
                    "recent_messages": [{"sender": "", "text": ch.get("preview", "")[:200]}],
                })

        # Collect metadata + preview from list view first
        chats_to_read = []
        for item in unread_els[:6]:
            try:
                aria = (await item.get_attribute("aria-label") or "").strip()
                name_el = await item.query_selector(
                    '[data-tid="chat-list-item-title"], '
                    '.fui-ChatListItem__header, '
                    '[class*="chatListItemTitle"], '
                    '[class*="itemHeader"]'
                )
                name = (await name_el.inner_text()).strip() if name_el else aria.split(",")[0]

                preview_el = await item.query_selector(
                    '[data-tid="chat-list-item-last-message"], '
                    '.fui-ChatListItem__subtitle, '
                    '[class*="chatListItemSubtitle"], '
                    '[class*="lastMessage"]'
                )
                preview = (await preview_el.inner_text()).strip() if preview_el else ""
                preview = " ".join(preview.split())[:250]

                aria_lower = aria.lower()
                chats_to_read.append({
                    "el":         item,
                    "chat":       name,
                    "is_dm":      "chat with" in aria_lower,
                    "is_mention": any(k in aria_lower for k in ["@mentioned", "mentioned you", "@ mentioned"]),
                    "preview":    preview,
                })
            except Exception:
                continue

        # Click into each chat to read actual messages
        for ch in chats_to_read:
            recent_msgs = []
            try:
                await ch["el"].click()
                await page.wait_for_timeout(3500)

                msg_els = await page.query_selector_all(
                    '[data-tid="message-body-content"], '
                    '[class*="messageBodyContent"], '
                    '.fui-ChatMessageBody, '
                    '[class*="messageBody"]'
                )
                seen_texts = set()
                for msg_el in msg_els[-8:]:
                    try:
                        # Sender: walk up to find sibling/parent with sender name
                        parent = await msg_el.evaluate_handle("el => el.closest('[data-tid=\"chat-message\"]') || el.parentElement?.parentElement")
                        sender_el = await page.query_selector(
                            '[data-tid="author-name"], [class*="senderName"], [class*="authorName"]'
                        ) if not parent else await parent.query_selector(
                            '[data-tid="author-name"], [class*="senderName"], [class*="authorName"]'
                        )
                        sender = (await sender_el.inner_text()).strip() if sender_el else ""
                        text   = " ".join((await msg_el.inner_text()).split())[:220]
                        if text and text not in seen_texts:
                            seen_texts.add(text)
                            recent_msgs.append({"sender": sender, "text": text})
                    except Exception:
                        continue
            except Exception:
                pass  # Click failed — fall back to preview only

            result["messages"].append({
                "chat":            ch["chat"],
                "unread":          True,
                "is_dm":           ch["is_dm"],
                "is_mention":      ch["is_mention"],
                "preview":         ch["preview"],
                "recent_messages": recent_msgs,
            })

    except Exception as e:
        result["error"] = str(e)
        result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "teams", e)
    finally:
        await page.close()
    return result


async def scrape_calendar(context: BrowserContext, email: str = "") -> dict:
    import datetime, time as _time, re as _re
    page = await context.new_page()
    result = {"service": "Calendar", "events": [], "error": None}
    # Use account-specific URL when email is provided so multi-account sessions always hit the right one
    _cal_url = (
        f"https://calendar.google.com/calendar/r/day?authuser={email}"
        if email else "https://calendar.google.com/calendar/r/day"
    )
    try:
        await page.goto(_cal_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(7000)  # GCal SPA needs more time

        if await _is_auth_wall(page):
            result["error"] = "not_logged_in"
            result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, f"calendar_{email or 'default'}", result["error"])
            return result

        today_start = int(datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp()) * 1000
        today_end   = int(datetime.datetime.combine(datetime.date.today(), datetime.time.max).timestamp()) * 1000
        now_ts      = int(_time.time() * 1000)

        # Pass 1: elements with explicit data-start-time (most accurate)
        event_els = list(await page.query_selector_all('[data-eventid][data-start-time]'))

        # Pass 2: any element with a data-eventid (broader net)
        if len(event_els) < 2:
            broader = await page.query_selector_all(
                'a[data-eventid], [role="button"][data-eventid], [data-eventid]'
            )
            seen_ids = set(id(e) for e in event_els)
            for el in broader:
                if id(el) not in seen_ids:
                    event_els.append(el)
                    seen_ids.add(id(el))

        # Pass 3: scan all aria-label elements for time patterns — catches modern GCal chips
        # that use neither data-eventid nor data-start-time
        if len(event_els) < 2:
            all_labeled = await page.query_selector_all('[role="button"][aria-label], a[aria-label], [data-eventchip]')
            seen_ids = set(id(e) for e in event_els)
            for el in all_labeled:
                aria = (await el.get_attribute("aria-label") or "").strip()
                # Must have a time pattern AND be long enough to be an event (not a nav button like "9 AM")
                if _re.search(r'\d{1,2}(?::\d{2})?\s*(?:AM|PM)', aria, _re.IGNORECASE) and len(aria) > 20:
                    if id(el) not in seen_ids:
                        event_els.append(el)
                        seen_ids.add(id(el))

        # Personal/recurring noise that should never appear in an intelligence briefing
        _NOISE_EVENTS = [
            "lunch time", "lunch!", "tea time", "tea break",
            "kytes entr", "add kytes", "fill your timesheet", "fill timesheet",
            "timesheet reminder", "timesheet entry",
            "bim kt", "continuing education renewal",
            "placeholder", "standup placeholder",
            "reminder- ", "reminder -",
            # Working location / status indicators (not actual meetings)
            "working location", "current location", "change working location",
            "update working location", "work from home", "out of office",
        ]
        # Single-word working location titles Google/Outlook create as all-day events
        _EXACT_NOISE_TITLES = {"home", "office", "wfh", "wfo", "remote", "away", "vacation day"}

        # Matches "4:30pm to 5:30pm" style ranges used by Google Calendar
        _RANGE_RE = _re.compile(
            r'(\d{1,2}(?::\d{2})?(?:am|pm)\s+to\s+\d{1,2}(?::\d{2})?(?:am|pm))',
            _re.IGNORECASE
        )

        seen = set()
        for el in event_els[:30]:
            try:
                start_ms = int(await el.get_attribute("data-start-time") or 0)
                end_ms   = int(await el.get_attribute("data-end-time") or start_ms)

                aria  = (await el.get_attribute("aria-label") or "").strip()
                inner = (await el.inner_text()).strip()

                # ── Extract clean title ──
                # GCal chip inner text: "4:30pm to 5:30pm\nEvent Title\nAttendee\nStatus..."
                inner_lines = [l.strip() for l in inner.split('\n') if l.strip()]
                title = ""
                if inner_lines:
                    # Skip first line if it's a time range (GCal chip format)
                    skip_first = bool(_RANGE_RE.match(inner_lines[0]))
                    title = inner_lines[1 if skip_first and len(inner_lines) > 1 else 0]
                if not title:
                    # Fall back to aria-label with time prefix stripped
                    title = _RANGE_RE.sub('', aria).lstrip(', ').split(',')[0]
                title = title.strip()[:100]
                if not title or title.lower() in ("event", "more") or title in seen:
                    continue
                if title.lower() in _EXACT_NOISE_TITLES:
                    continue
                if any(n in title.lower() for n in _NOISE_EVENTS):
                    continue

                time_str     = ""
                start_ts_sec = None
                end_ts_sec   = None
                status       = "upcoming"

                if start_ms:
                    if not (today_start <= start_ms <= today_end):
                        continue
                    start_dt = datetime.datetime.fromtimestamp(start_ms / 1000)
                    end_dt   = datetime.datetime.fromtimestamp(end_ms / 1000)
                    time_str = f"{start_dt.strftime('%-I:%M%p').lower()} – {end_dt.strftime('%-I:%M%p').lower()}"
                    start_ts_sec = start_ms // 1000
                    end_ts_sec   = end_ms // 1000
                    if end_ms < now_ts:
                        status = "concluded"
                    elif start_ms <= now_ts <= end_ms:
                        status = "ongoing"
                else:
                    # No data-start-time — extract time from aria-label OR inner text
                    # Try "X to Y" range first (most precise)
                    range_m = _RANGE_RE.search(aria) or _RANGE_RE.search(inner)
                    if range_m:
                        time_str = range_m.group(1)
                    else:
                        # Fall back to single time mention
                        simple_m = (
                            _re.search(r'(\d{1,2}(?::\d{2})?\s*[AP]M)', aria, _re.IGNORECASE) or
                            _re.search(r'(\d{1,2}(?::\d{2})?(?:am|pm))', inner, _re.IGNORECASE)
                        )
                        if simple_m:
                            time_str = simple_m.group(1).upper()

                seen.add(title)
                result["events"].append({
                    "title":    title,
                    "time":     time_str,
                    "start_ts": start_ts_sec,
                    "end_ts":   end_ts_sec,
                    "status":   status,
                })
            except Exception:
                continue

        # All-day events are intentionally excluded — they are status/location indicators,
        # not schedule items, and are filtered out before display anyway.

    except Exception as e:
        result["error"] = str(e)
        result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, f"calendar_{email or 'default'}", e)
    finally:
        await page.close()
    return result


async def scrape_outlook_calendar(context: BrowserContext) -> dict:
    import re as _re, datetime as _dt, time as _time
    page = await context.new_page()
    result = {"service": f"{OUTLOOK_LABEL} Calendar", "events": [], "error": None}

    _OWA_LOGIN_WALLS = ["login.live.com", "login.microsoftonline.com", "login.microsoft.com", "login.windows.net"]

    # Event element selectors — covers classic OWA, new OWA, new Outlook (cloud.microsoft)
    # New Outlook uses [data-eventid] on event containers; also try Fluent UI class patterns
    _OWA_EVENT_SEL = (
        '[data-eventid], [class*="eventItem"][role="button"], '
        '[class*="calendarEvent"], [class*="event-item"], '
        '[data-automation-id*="CalendarEvent"], [class*="EventCard"], '
        '[class*="event"][role="button"], [class*="Event"][role="button"], '
        '[class*="fui-"][class*="event"], [class*="fui-"][class*="Event"]'
    )

    try:
        # Navigate directly to configured Outlook tenant calendar (no redirect guessing)
        cal_url = f"{OUTLOOK_URL}/calendar/view/Day"
        await page.goto(cal_url, wait_until="domcontentloaded", timeout=30000)
        print(f"[outlook-cal] navigated to: {cal_url} → landed: {page.url}", flush=True)

        # Fast-path: if the context was already authenticated by scrape_outlook (same ctx),
        # we land directly on the calendar with no SSO redirect needed.
        already_authenticated = any(d in page.url for d in [
            "outlook.cloud.microsoft", "outlook.office.com", "outlook.office365.com"
        ]) and "calendar" in page.url.lower()

        if already_authenticated:
            # Wait for calendar EVENT elements only — NOT toolbar buttons like "Day view"
            # which appear immediately and would cause the wait to resolve before events load.
            print(f"[outlook-cal] already authenticated, waiting for event elements", flush=True)
            try:
                await page.wait_for_selector(
                    _OWA_EVENT_SEL + ', [data-eventid], [role="button"][aria-label*="M"]',
                    timeout=25000,
                )
                print(f"[outlook-cal] event elements appeared", flush=True)
            except Exception:
                # Selector timed out — events may use different HTML; give extra settle time
                print(f"[outlook-cal] event selector timed out, settling 8s", flush=True)
                await page.wait_for_timeout(8000)
        else:
            # May need to ride through an Okta SSO redirect before the calendar appears
            try:
                await page.wait_for_selector(
                    _OWA_EVENT_SEL + ', [aria-label="Day view"], input[type="email"]',
                    timeout=25000,
                )
            except Exception:
                pass

            if await _is_auth_wall(page):
                print(f"[outlook-cal] SSO redirect detected — waiting for auto-completion: {page.url}", flush=True)
                try:
                    await page.wait_for_url(
                        lambda url: "outlook.cloud.microsoft" in url or "outlook.office" in url or "outlook.office365.com" in url,
                        timeout=45000,
                    )
                    print(f"[outlook-cal] SSO completed, landed: {page.url}", flush=True)
                    await page.wait_for_timeout(2000)
                except Exception:
                    print(f"[outlook-cal] SSO did not auto-complete, still at: {page.url}", flush=True)
                    result["error"] = "not_logged_in"
                    result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "calendar_outlook", result["error"])
                    return result

            # SSO may land on /mail/ — navigate explicitly to the calendar
            if "calendar" not in page.url.lower():
                print(f"[outlook-cal] not on calendar page ({page.url}), navigating to {cal_url}", flush=True)
                await page.goto(cal_url, wait_until="domcontentloaded", timeout=30000)

            # Settle for a fresh Outlook React SPA load
            await page.wait_for_timeout(5000)

        if await _is_auth_wall(page):
            result["error"] = "not_logged_in"
            result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "calendar_outlook", result["error"])
            return result

        # ── Step 3: Ensure we're in Day view (not week/month) ──
        if "day" not in page.url.lower():
            for sel in ['button[aria-label="Day view"]', 'button[aria-label="Day"]',
                        '[title="Day view"]', '[title="Day"]']:
                day_btn = await page.query_selector(sel)
                if day_btn:
                    await day_btn.click()
                    await page.wait_for_timeout(2000)
                    break

        # Sanity-check for onboarding/login screens (catches SSO flows _is_auth_wall may miss)
        page_text = (await page.inner_text('body'))[:3000].lower()
        _OWA_ONBOARD = ["slide 1", "watch video", "take a tour"]
        if any(s in page_text for s in _OWA_ONBOARD + _AUTH_BODY_SIGNALS):
            result["error"] = "not_logged_in"
            result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "calendar_outlook", result["error"])
            return result

        # ── Step 4: Scrape events ──
        today          = _dt.date.today()
        today_start_ms = int(_dt.datetime.combine(today, _dt.time.min).timestamp()) * 1000
        today_end_ms   = int(_dt.datetime.combine(today, _dt.time.max).timestamp()) * 1000
        now_ms         = int(_time.time() * 1000)

        # Event element selectors — cascade from most specific to broadest
        event_els = []
        for sel in [
            '[data-eventid]',
            '[class*="eventItem"][role="button"]',
            '[class*="calendarEvent"]',
            '[class*="event-item"]',
            '[data-automation-id*="CalendarEvent"]',
            '[class*="EventCard"]',
            '[class*="eventWrapper"]',
        ]:
            found = await page.query_selector_all(sel)
            if found:
                event_els = found
                break

        # Diagnostic when primary selectors find nothing
        if not event_els:
            _cal_diag = await page.evaluate("""() => {
                const body = document.body.innerText;
                // Search for known calendar keywords in the entire DOM
                const allEls = Array.from(document.querySelectorAll('*'));
                const eventLike = allEls.filter(e => {
                    const t = (e.innerText || '').trim();
                    return t.length > 10 && t.length < 300 &&
                           /\\d{1,2}(?::\\d{2})?\\s*[AP]M/i.test(t) &&
                           e.children.length < 5;
                }).slice(0, 5).map(e => e.innerText.trim().substring(0, 80));
                return {
                    url: location.href,
                    bodyLen: body.length,
                    hasStandup: body.toLowerCase().includes('standup'),
                    hasMeeting: body.toLowerCase().includes('meeting'),
                    bodyMid: body.substring(400, 1200),
                    allButtons: document.querySelectorAll('[role="button"]').length,
                    tabindexEls: document.querySelectorAll('[tabindex]').length,
                    eventLikeCandidates: eventLike,
                };
            }""")
            print(f"[outlook-cal] 0 primary matches — diag: {_cal_diag}", flush=True)

        # Final fallback: any element whose aria-label contains a time pattern
        if not event_els:
            # New Outlook events may be div/span without role="button" — scan all aria-labels
            all_labeled = await page.query_selector_all('[aria-label], [title]')
            OWA_RANGE_RE_SCAN = _re.compile(r'\d{1,2}(?::\d{2})?\s*(?:AM|PM)', _re.IGNORECASE)
            for el in all_labeled:
                label = await el.get_attribute("aria-label") or await el.get_attribute("title") or ""
                if OWA_RANGE_RE_SCAN.search(label) and len(label) > 20:
                    event_els.append(el)

        skip_phrases = [
            "previous", "next", "today", "month", "week", "day", "agenda",
            "new event", "new message", "search", "settings", "more options",
            "add calendar", "mini calendar", "working location", "work from home",
            "no meetings friday", "out of office", "current location",
        ]
        _OWA_EXACT_NOISE = {"home", "office", "wfh", "wfo", "remote", "away"}

        OWA_RANGE_RE = _re.compile(
            r'(\d{1,2}(?::\d{2})?\s*[AP]M)\s*[-–—to]+\s*(\d{1,2}(?::\d{2})?\s*[AP]M)',
            _re.IGNORECASE
        )

        seen = set()
        for el in event_els[:30]:
            try:
                aria  = (await el.get_attribute("aria-label") or "").strip()
                inner = (await el.inner_text()).strip().replace("\n", " ")

                # Use whichever is more descriptive as label, then clean
                label = aria if len(aria) >= len(inner) else inner
                label = label.strip()

                if not label or len(label) < 5 or label in seen:
                    continue
                if label.lower().strip() in _OWA_EXACT_NOISE:
                    continue
                if any(w in label.lower() for w in skip_phrases):
                    continue

                # Extract time range from label ("10:00 AM – 11:00 AM, Meeting")
                time_str     = ""
                start_ts_sec = None
                end_ts_sec   = None
                status       = "upcoming"

                # Check data-start-time first
                start_ms_attr = await el.get_attribute("data-start-time")
                if start_ms_attr:
                    start_ms = int(start_ms_attr)
                    end_ms   = int(await el.get_attribute("data-end-time") or start_ms_attr)
                    if today_start_ms <= start_ms <= today_end_ms:
                        start_dt = _dt.datetime.fromtimestamp(start_ms / 1000)
                        end_dt   = _dt.datetime.fromtimestamp(end_ms / 1000)
                        time_str     = f"{start_dt.strftime('%-I:%M%p').lower()} – {end_dt.strftime('%-I:%M%p').lower()}"
                        start_ts_sec = start_ms // 1000
                        end_ts_sec   = end_ms // 1000
                        status = "concluded" if end_ms < now_ms else ("ongoing" if start_ms <= now_ms <= end_ms else "upcoming")
                    else:
                        continue  # Not today
                else:
                    range_m = OWA_RANGE_RE.search(label)
                    if range_m:
                        time_str = f"{range_m.group(1)} – {range_m.group(2)}"
                    else:
                        simple_m = _re.search(r'(\d{1,2}(?::\d{2})?\s*[AP]M)', label, _re.IGNORECASE)
                        if simple_m:
                            time_str = simple_m.group(1).upper()

                # Clean the title: strip leading time range if present
                title = OWA_RANGE_RE.sub('', label).lstrip(' ,.-–—').split(',')[0].strip()[:100]
                if not title:
                    title = label[:100]

                seen.add(label)
                result["events"].append({
                    "title":    title,
                    "time":     time_str,
                    "start_ts": start_ts_sec,
                    "end_ts":   end_ts_sec,
                    "status":   status,
                })
            except Exception:
                continue

        print(f"[outlook-cal] CSS selectors found {len(result['events'])} event(s)", flush=True)

        # JS fallback: extract events directly when CSS selectors found nothing
        if not result["events"]:
            js_events = await page.evaluate(OUTLOOK_CALENDAR_FALLBACK_JS)
            print(f"[outlook-cal] JS fallback found {len(js_events)} event(s)", flush=True)
            _CAL_NOISE = {"home", "office", "wfh", "wfo", "remote", "app launcher",
                          "calendar", "copilot", "teams", "excel", "word", "powerpoint",
                          "onenote", "sharepoint", "outlook", "onedrive", "loop", "planner"}
            for ev in js_events:
                title = (ev.get("title") or "").strip()
                time  = (ev.get("time") or "").strip()
                if not title:
                    continue
                if title.lower() in _CAL_NOISE:
                    continue
                result["events"].append({
                    "title":    title,
                    "time":     time,
                    "start_ts": None,
                    "end_ts":   None,
                    "status":   "upcoming",
                })

        # Body-text parsing fallback: extract event titles from body.innerText,
        # then click each event element to open its detail popup and read the time.
        # This is reliable even when times are only encoded as CSS positioning.
        if not result["events"]:
            # Step 1: extract titles via body.innerText (after the 0-23 hour rail)
            _raw_titles = await page.evaluate(OUTLOOK_CALENDAR_BODY_TEXT_JS)
            print(f"[outlook-cal] body-text titles: {_raw_titles}", flush=True)

            # Step 2: for each title, click the event element to get its time from the popup
            _TIME_RE = _re.compile(r'\d{1,2}(?::\d{2})?\s*[AP]M', _re.IGNORECASE)
            _POPUP_SELS = [
                '[class*="eventPeek"]', '[class*="EventPeek"]',
                '[class*="eventCard"][tabindex]', '[class*="EventCallout"]',
                '[role="dialog"]', '[class*="Callout"]', '[class*="callout"]',
            ]

            for _title in (_raw_titles or [])[:8]:
                _time_str = ""
                _start_ts = None
                _end_ts = None
                _status = "upcoming"
                try:
                    # Find the element position by walking text nodes
                    _pos = await page.evaluate(
                        """(title) => {
                            const walker = document.createTreeWalker(document.body, 4, null);
                            let node;
                            while (node = walker.nextNode()) {
                                if (node.nodeValue.trim() === title) {
                                    const el = node.parentElement;
                                    if (!el) continue;
                                    const r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0)
                                        return { x: r.left + r.width/2, y: r.top + r.height/2, found: true };
                                }
                            }
                            return { found: false };
                        }""",
                        _title
                    )
                    print(f"[outlook-cal] '{_title}' pos={_pos}", flush=True)

                    if _pos.get('found'):
                        await page.mouse.click(_pos['x'], _pos['y'])
                        await page.wait_for_timeout(900)

                        # Scrape popup / detail card for time text
                        _popup_text = await page.evaluate(
                            """(sels) => {
                                for (const sel of sels) {
                                    const el = document.querySelector(sel);
                                    if (el && el.innerText.length > 15) return el.innerText;
                                }
                                // Fallback: look for any newly appeared element with a time pattern
                                const all = Array.from(document.querySelectorAll('*'));
                                const fresh = all.find(e =>
                                    e.innerText && /\d{1,2}(?::\d{2})?\s*[AP]M/i.test(e.innerText) &&
                                    e.innerText.length < 500 &&
                                    (e.style.zIndex > 0 || window.getComputedStyle(e).zIndex > 0)
                                );
                                return fresh ? fresh.innerText : '';
                            }""",
                            _POPUP_SELS
                        )
                        print(f"[outlook-cal] popup for '{_title}': {repr(_popup_text[:200])}", flush=True)

                        if _popup_text and _title.lower() in _popup_text.lower():
                            _parsed_time = _parse_outlook_calendar_time(_popup_text)
                            _time_str = _parsed_time.get("time", "")
                            _start_ts = _parsed_time.get("start_ts")
                            _end_ts = _parsed_time.get("end_ts")
                            _status = _parsed_time.get("status", "upcoming")
                        elif _popup_text:
                            print(f"[outlook-cal] ignored unrelated popup for '{_title}'", flush=True)

                        await page.keyboard.press('Escape')
                        await page.wait_for_timeout(300)
                except Exception as _ce:
                    print(f"[outlook-cal] click error for '{_title}': {_ce}", flush=True)

                result["events"].append({
                    "title":    _title,
                    "time":     _time_str,
                    "start_ts": _start_ts,
                    "end_ts":   _end_ts,
                    "status":   _status,
                })
    except Exception as e:
        result["error"] = str(e)
        result["diagnostic_path"] = await reliability.capture_page_diagnostic(page, "calendar_outlook", e)
    finally:
        await page.close()
    return result


LOGIN_URLS = {
    # AccountChooser pre-selects the correct email — adds/switches account if not already signed in
    "gmail1":  f"https://accounts.google.com/AccountChooser?Email={GMAIL_1_EMAIL}&continue=https://mail.google.com",
    "gmail2":  f"https://accounts.google.com/AccountChooser?Email={GMAIL_2_EMAIL}&continue=https://mail.google.com",
    # With a corporate Okta portal, log in there — all Microsoft apps flow from it
    "outlook": MYAPPS_URL or f"{OUTLOOK_URL}/mail/",
    "slack":   "https://app.slack.com",
    "teams":   MYAPPS_URL or "https://teams.microsoft.com",
}

# Maps myapps profiles → (tile label, MS landing URL check)
_MYAPPS_TILE_MAP = {
    "outlook": (
        "O365 - Mail",
        lambda u: any(d in u for d in ["outlook.cloud.microsoft", "outlook.office.com", "outlook.office365.com"]),
    ),
    "teams": (
        "O365 - Teams",
        lambda u: "teams.microsoft.com" in u and "signout" not in u,
    ),
}


async def _myapps_navigate_to_app(ctx: BrowserContext, portal_page, profile: str) -> None:
    """
    Called during setup after navigating to the Okta portal (MYAPPS_URL).
    Waits for the user to complete Okta login (UserHome appears), then automatically
    clicks the app tile so the Microsoft session cookie is also saved before ctx.close().
    """
    tile_info = _MYAPPS_TILE_MAP.get(profile)
    if not tile_info:
        return
    tile_label, target_check = tile_info

    print(f"[setup] waiting for Okta login on myapps for '{profile}'…", flush=True)
    try:
        # Wait up to 5 min for the user to finish entering credentials
        await portal_page.wait_for_url(
            lambda u: "UserHome" in u or "session_hint=AUTHENTICATED" in u,
            timeout=300_000,
        )
    except Exception:
        print(f"[setup] timed out waiting for Okta UserHome for '{profile}'", flush=True)
        return

    # Wait for the tile grid to actually render before searching
    try:
        await portal_page.wait_for_selector(
            '[title], .app-logo, [class*="app-tile"], [class*="AppTile"]',
            timeout=8000,
        )
    except Exception:
        pass
    await portal_page.wait_for_timeout(1500)

    # Find the tile: exact title → partial title → text content scan
    tile = await portal_page.query_selector(f'[title="{tile_label}"]')
    if not tile:
        # Partial match on title (handles minor label variations)
        all_titled = await portal_page.query_selector_all('[title]')
        for el in all_titled:
            t = (await el.get_attribute('title') or '').strip()
            if tile_label.lower() in t.lower():
                tile = el
                break
    if not tile:
        for el in await portal_page.query_selector_all('a[href], [role="link"], [role="button"]'):
            try:
                text = (await el.inner_text() or '').strip()
                if tile_label.lower() in text.lower():
                    tile = el
                    break
            except Exception:
                continue

    if not tile:
        print(f"[setup] tile '{tile_label}' not found — user must navigate manually", flush=True)
        return

    print(f"[setup] auto-clicking '{tile_label}' to save MS session cookie…", flush=True)
    try:
        async with ctx.expect_page(timeout=8000) as pg_info:
            await tile.click()
        app_page = await pg_info.value
        await app_page.wait_for_url(target_check, timeout=60000)
        await app_page.bring_to_front()
        print(f"[setup] '{profile}' MS app ready: {app_page.url[:80]}", flush=True)
    except Exception:
        # Tile opened in same tab
        try:
            await portal_page.wait_for_url(target_check, timeout=60000)
            await portal_page.bring_to_front()
            print(f"[setup] '{profile}' MS app ready (same tab): {portal_page.url[:80]}", flush=True)
        except Exception as e:
            print(f"[setup] '{profile}' auto-navigate failed: {e}", flush=True)


LOGIN_DONE_CHECKS = {
    "gmail1":  lambda url: "mail.google.com/mail" in url,
    "gmail2":  lambda url: "mail.google.com/mail" in url,
    "outlook": lambda url: any(d in url for d in [
        "outlook.live.com", "outlook.office.com",
        "outlook.office365.com", "outlook.cloud.microsoft",
    ]),
    "slack":   lambda url: "app.slack.com/client" in url,
    "teams":   lambda url: "teams.microsoft.com" in url and (
        "/v2" in url or "/_#" in url or "/teams/" in url or url.rstrip("/").endswith("teams.microsoft.com")
    ),
}


async def relogin_one_service(profile: str, advance_queue: asyncio.Queue, status_callback: Callable):
    """Open a visible browser for a single service so the user can re-authenticate."""
    url = LOGIN_URLS.get(profile, "https://mail.google.com")
    async with async_playwright() as pw:
        await status_callback(profile, "awaiting_login")
        async with reliability.profile_lock(profile, wait=True):
            ctx = await pw.chromium.launch_persistent_context(
                str(SESSION_DIR / profile),
                headless=False,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=0)
            except Exception:
                pass
            await page.bring_to_front()
            # Wait for user to confirm login via "LOGGED IN — NEXT" button.
            # For myapps profiles: log into Okta, click the app tile (e.g. O365 - Mail),
            # wait until the MS app loads, then click "Logged In — Next" in JARVIS.
            await advance_queue.get()
            await ctx.close()


async def setup_logins(status_callback: Callable, advance_queue: asyncio.Queue):
    """Open a visible browser per service; advances when user puts to advance_queue."""
    async with async_playwright() as pw:
        for profile, url in LOGIN_URLS.items():
            await status_callback(profile, "awaiting_login")
            async with reliability.profile_lock(profile, wait=True):
                ctx = await pw.chromium.launch_persistent_context(
                    str(SESSION_DIR / profile),
                    headless=False,
                    viewport={"width": 1280, "height": 800},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = await ctx.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=0)
                except Exception:
                    pass
                await page.bring_to_front()
                # Block until the user clicks "LOGGED IN – NEXT" in the JARVIS UI.
                # For myapps profiles: log into Okta, click the O365 tile to land on
                # Outlook / Teams, then click "Logged In — Next". The persistent context
                # saves cookies from ALL tabs opened during this session on ctx.close().
                await advance_queue.get()
                await status_callback(profile, "authed")
                await ctx.close()


SERVICE_SCRAPER_MAP = {
    "gmail_1": ("gmail1", lambda ctx: scrape_gmail(ctx, GMAIL_1_EMAIL, f"{GMAIL_1_LABEL} Gmail")),
    "gmail_2": ("gmail2", lambda ctx: scrape_gmail(ctx, GMAIL_2_EMAIL, f"{GMAIL_2_LABEL} Gmail")),
    "outlook": ("outlook", scrape_outlook),
    "slack":   ("slack",   scrape_slack),
    "teams":   ("teams",   scrape_teams),
    "calendar_gmail1": ("gmail1",  lambda ctx: scrape_calendar(ctx, GMAIL_1_EMAIL)),
    "calendar_gmail2": ("gmail2",  lambda ctx: scrape_calendar(ctx, GMAIL_2_EMAIL)),
    "calendar_outlook": ("outlook", scrape_outlook_calendar),
}


_ALWAYS_VISIBLE_PROFILES = {"outlook", "teams"}

async def scrape_one_service(service_key: str) -> dict | None:
    entry = SERVICE_SCRAPER_MAP.get(service_key)
    if not entry:
        return None
    profile, scraper = entry
    session_path = SESSION_DIR / profile
    headless = False if profile in _ALWAYS_VISIBLE_PROFILES else _is_logged_in_session(session_path)
    async with async_playwright() as pw:
        return await _run_service_with_context(
            pw,
            service_key,
            profile,
            scraper,
            headless=headless,
            wait_for_lock=False,
        )


async def _run_scraper_with_timeout(service_key: str, profile: str, scraper: Callable, ctx: BrowserContext) -> dict:
    started = time.perf_counter()
    last_failure: dict | None = None
    attempts = max(1, reliability.DEFAULT_RETRIES + 1)
    for attempt in range(attempts):
        try:
            result = await asyncio.wait_for(scraper(ctx), timeout=reliability.DEFAULT_SERVICE_TIMEOUT)
            if result and not result.get("error"):
                fresh = reliability.attach_success_meta(result, service_key, profile, started)
                reliability.persist_if_fresh(service_key, fresh)
                return fresh
            last_failure = reliability.attach_failure_meta(
                result,
                service_key,
                profile,
                started,
                (result or {}).get("error") or "empty_result",
                diagnostic_path=(result or {}).get("diagnostic_path"),
            )
        except Exception as exc:
            last_failure = reliability.attach_failure_meta(None, service_key, profile, started, exc)
        if attempt < attempts - 1:
            await asyncio.sleep(1.5)

    assert last_failure is not None
    return reliability.with_stale_fallback(service_key, profile, last_failure, started)


async def _run_service_with_context(
    pw,
    service_key: str,
    profile: str,
    scraper: Callable,
    *,
    headless: bool,
    wait_for_lock: bool = True,
) -> dict:
    started = time.perf_counter()
    async with reliability.profile_lock(profile, wait=wait_for_lock) as acquired:
        if not acquired:
            failure = reliability.attach_failure_meta(
                None,
                service_key,
                profile,
                started,
                "profile_busy",
            )
            return reliability.with_stale_fallback(service_key, profile, failure, started)
        ctx = await _get_context(pw, profile, headless)
        try:
            return await _run_scraper_with_timeout(service_key, profile, scraper, ctx)
        finally:
            await ctx.close()


async def _run_service_in_existing_context(
    service_key: str,
    profile: str,
    scraper: Callable,
    ctx: BrowserContext,
) -> dict:
    return await _run_scraper_with_timeout(service_key, profile, scraper, ctx)


async def _status(status_callback: Callable, service: str, data: dict | None = None, state: str | None = None):
    if state is None:
        state = "done" if data and not data.get("error") and not (data.get("meta") or {}).get("stale") else "error"
    await status_callback(service, state)


async def run_all_scrapers(status_callback: Callable) -> dict:
    """Run independent browser profiles concurrently while serializing work per profile."""
    all_data = {}

    async with async_playwright() as pw:
        profiles = {
            "gmail1":  SESSION_DIR / "gmail1",
            "gmail2":  SESSION_DIR / "gmail2",
            "outlook": SESSION_DIR / "outlook",
            "slack":   SESSION_DIR / "slack",
            "teams":   SESSION_DIR / "teams",
        }
        headless_map = {k: _is_logged_in_session(v) for k, v in profiles.items()}

        async def gmail1_group():
            out = {}
            async with reliability.profile_lock("gmail1", wait=True):
                ctx = await _get_context(pw, "gmail1", headless_map["gmail1"])
                try:
                    await status_callback("calendar_gmail1", "scanning")
                    cal1 = await _run_service_in_existing_context(
                        "calendar_gmail1",
                        "gmail1",
                        lambda c: scrape_calendar(c, GMAIL_1_EMAIL),
                        ctx,
                    )
                    cal1["service"] = f"{GMAIL_1_LABEL} Calendar"
                    out["calendar_gmail1"] = cal1
                    await _status(status_callback, "calendar_gmail1", cal1)

                    await status_callback("gmail_1_mail", "scanning")
                    await status_callback("gmail_1_chat", "scanning")
                    gmail1 = await _run_service_in_existing_context(
                        "gmail_1",
                        "gmail1",
                        lambda c: scrape_gmail(c, GMAIL_1_EMAIL, f"{GMAIL_1_LABEL} Gmail"),
                        ctx,
                    )
                    out["gmail_1"] = gmail1
                    state = "done" if not gmail1.get("error") and not (gmail1.get("meta") or {}).get("stale") else "error"
                    await status_callback("gmail_1_mail", state)
                    await status_callback("gmail_1_chat", state)
                finally:
                    await ctx.close()
            return out

        async def gmail2_group():
            out = {}
            async with reliability.profile_lock("gmail2", wait=True):
                ctx = await _get_context(pw, "gmail2", headless_map["gmail2"])
                try:
                    await status_callback("calendar_gmail2", "scanning")
                    cal2 = await _run_service_in_existing_context(
                        "calendar_gmail2",
                        "gmail2",
                        lambda c: scrape_calendar(c, GMAIL_2_EMAIL),
                        ctx,
                    )
                    cal2["service"] = f"{GMAIL_2_LABEL} Calendar"
                    out["calendar_gmail2"] = cal2
                    await _status(status_callback, "calendar_gmail2", cal2)

                    await status_callback("gmail_2_mail", "scanning")
                    gmail2 = await _run_service_in_existing_context(
                        "gmail_2",
                        "gmail2",
                        lambda c: scrape_gmail(c, GMAIL_2_EMAIL, f"{GMAIL_2_LABEL} Gmail"),
                        ctx,
                    )
                    out["gmail_2"] = gmail2
                    await _status(status_callback, "gmail_2_mail", gmail2)
                finally:
                    await ctx.close()
            return out

        async def slack_group():
            await status_callback("slack", "scanning")
            slack = await _run_service_with_context(
                pw,
                "slack",
                "slack",
                scrape_slack,
                headless=headless_map["slack"],
                wait_for_lock=True,
            )
            await _status(status_callback, "slack", slack)
            return {"slack": slack}

        async def outlook_group():
            out = {}
            async with reliability.profile_lock("outlook", wait=True):
                ctx = await _get_context(pw, "outlook", headless=False)
                try:
                    await status_callback("outlook", "scanning")
                    await status_callback("calendar_outlook", "scanning")
                    outlook = await _run_service_in_existing_context("outlook", "outlook", scrape_outlook, ctx)
                    out["outlook"] = outlook
                    await _status(status_callback, "outlook", outlook)

                    cal_ol = await _run_service_in_existing_context("calendar_outlook", "outlook", scrape_outlook_calendar, ctx)
                    out["calendar_outlook"] = cal_ol
                    await _status(status_callback, "calendar_outlook", cal_ol)
                finally:
                    await ctx.close()
            return out

        async def teams_group():
            await status_callback("teams", "scanning")
            teams = await _run_service_with_context(
                pw,
                "teams",
                "teams",
                scrape_teams,
                headless=False,
                wait_for_lock=True,
            )
            await _status(status_callback, "teams", teams)
            return {"teams": teams}

        results = await asyncio.gather(
            gmail1_group(),
            gmail2_group(),
            slack_group(),
            outlook_group(),
            teams_group(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                print(f"[run-all] profile group failed: {result}", flush=True)
                continue
            all_data.update(result)

    return all_data
