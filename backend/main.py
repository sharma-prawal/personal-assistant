import asyncio
import datetime
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
import edge_tts
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).parent / ".env")

from assistant import generate_briefing, generate_action_items, get_greeting, answer_question
from browser_agent import run_all_scrapers, setup_logins, scrape_one_service, relogin_one_service
import db
import reliability

app = FastAPI()
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_MINUTES", "15")) * 60
_BRIEFING_TIMES = {t.strip() for t in os.getenv("BRIEFING_TIMES", "").split(",") if t.strip()}
_connected_ws: set[WebSocket] = set()

# Services polled in background — skip calendars (they don't have real-time messages)
POLL_SERVICES = ["gmail_1", "gmail_2", "outlook", "slack", "teams"]

# Human-readable source labels for each poll key
POLL_LABELS = {
    "gmail_1": os.getenv("GMAIL_1_LABEL", "Gmail 1"),
    "gmail_2": os.getenv("GMAIL_2_LABEL", "Gmail 2"),
    "outlook": os.getenv("OUTLOOK_LABEL", "Outlook"),
    "slack":   "Slack",
    "teams":   "Teams",
}


def _time_str_to_ts(time_str: str) -> int | None:
    """Parse '4:30 PM' or '4:30PM' → today's unix timestamp. Returns None if unparseable."""
    if not time_str:
        return None
    m = re.search(r'(\d{1,2}):?(\d{2})?\s*([AP]M)', time_str, re.IGNORECASE)
    if not m:
        return None
    try:
        h = int(m.group(1))
        mins = int(m.group(2) or 0)
        ampm = m.group(3).upper()
        if ampm == "PM" and h != 12:
            h += 12
        if ampm == "AM" and h == 12:
            h = 0
        t = datetime.time(h, mins)
        dt = datetime.datetime.combine(datetime.date.today(), t)
        return int(dt.timestamp())
    except Exception:
        return None


def _domain_label(email: str) -> str:
    if not email or "@" not in email:
        return email or "Account"
    return email.split("@")[1].split(".")[0].capitalize()


# Maps any service key to the Playwright profile that owns its session
SERVICE_PROFILE_MAP = {
    "gmail_1": "gmail1", "gmail_1_mail": "gmail1", "gmail_1_chat": "gmail1", "calendar_gmail1": "gmail1",
    "gmail_2": "gmail2", "gmail_2_mail": "gmail2", "calendar_gmail2": "gmail2",
    "slack": "slack",
    "outlook": "outlook", "calendar_outlook": "outlook",
    "teams": "teams",
}

ACCOUNT_NAMES = {
    "gmail1":  os.getenv("GMAIL_1_EMAIL", "Gmail Account 1"),
    "gmail2":  os.getenv("GMAIL_2_EMAIL", "Gmail Account 2"),
    "outlook": os.getenv("OUTLOOK_EMAIL", "Outlook"),
}

ACCOUNT_LABELS = {
    "gmail1":  os.getenv("GMAIL_1_LABEL", _domain_label(os.getenv("GMAIL_1_EMAIL", ""))),
    "gmail2":  os.getenv("GMAIL_2_LABEL", _domain_label(os.getenv("GMAIL_2_EMAIL", ""))),
    "outlook": os.getenv("OUTLOOK_LABEL", _domain_label(os.getenv("OUTLOOK_EMAIL", ""))),
}


def _delta(fresh: dict, prev: dict) -> list[dict]:
    """Return new items in fresh that weren't in prev."""
    new_items = []

    # New emails
    fresh_emails = {(e.get("sender", ""), e.get("subject", "")) for e in fresh.get("emails", [])}
    prev_emails  = {(e.get("sender", ""), e.get("subject", "")) for e in prev.get("emails",  [])}
    for sender, subject in (fresh_emails - prev_emails):
        if sender or subject:
            new_items.append({"kind": "email", "from": sender, "subject": subject})

    # New chat messages
    fresh_chats = {(c.get("from", ""), c.get("preview", "")[:60]) for c in fresh.get("chats", [])}
    prev_chats  = {(c.get("from", ""), c.get("preview", "")[:60]) for c in prev.get("chats",  [])}
    for frm, preview in (fresh_chats - prev_chats):
        if frm:
            new_items.append({"kind": "chat", "from": frm, "preview": preview})

    # Increased unread counts in Slack/Teams
    def _count(value) -> int:
        if isinstance(value, bool):
            return 1 if value else 0
        try:
            return int(value)
        except Exception:
            return 1 if value else 0

    fresh_msgs = {(m.get("channel") or m.get("chat", "")): _count(m.get("unread", 0)) for m in fresh.get("messages", [])}
    prev_msgs  = {(m.get("channel") or m.get("chat", "")): _count(m.get("unread", 0)) for m in prev.get("messages",  [])}
    for channel, count in fresh_msgs.items():
        increase = count - prev_msgs.get(channel, 0)
        if increase > 0 and channel:
            new_items.append({"kind": "message", "channel": channel, "count": increase})

    return new_items


async def _polling_loop(ws: WebSocket, last_seen: dict):
    """Background task: poll messaging services and push diffs to the frontend."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        for key in POLL_SERVICES:
            try:
                fresh = await scrape_one_service(key)
                if not fresh:
                    continue
                await ws.send_json({"type": "health_update", "services": {key: _health_payload(key, fresh)}})
                if fresh.get("error"):
                    continue
                if (fresh.get("meta") or {}).get("stale"):
                    continue
                prev = last_seen.get(key, {})
                new_items = _delta(fresh, prev)
                # Update baseline regardless of whether there are new items
                last_seen[key] = fresh
                if new_items:
                    await ws.send_json({
                        "type":          "notification",
                        "service":       key,
                        "service_label": POLL_LABELS.get(key, key),
                        "items":         new_items[:5],
                        "count":         len(new_items),
                    })
            except Exception:
                try:
                    await ws.send_json({
                        "type": "health_update",
                        "services": {key: {"service_key": key, "status": "error", "error": "poll_failed"}},
                    })
                except Exception:
                    pass
                continue  # Never crash the poll loop


def _health_payload(service_key: str, data: dict) -> dict:
    meta = data.get("meta") or {}
    return {
        "service_key": service_key,
        "service": data.get("service", service_key),
        "status": meta.get("status") or ("error" if data.get("error") else "ok"),
        "error": data.get("error"),
        "error_code": meta.get("error_code"),
        "auth_status": meta.get("auth_status"),
        "duration_ms": meta.get("duration_ms"),
        "item_count": meta.get("item_count", reliability.item_count(data)),
        "confidence": meta.get("confidence"),
        "stale": bool(meta.get("stale")),
        "last_success_ts": meta.get("last_success_ts"),
        "diagnostic_path": meta.get("diagnostic_path"),
        "run_id": meta.get("run_id"),
    }


def _health_index(scraped_data: dict) -> dict:
    return {key: _health_payload(key, data) for key, data in scraped_data.items() if data}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _connected_ws.add(ws)
    await ws.send_json({"type": "greeting", "text": get_greeting()})
    await ws.send_json({"type": "config", "accounts": ACCOUNT_NAMES, "labels": ACCOUNT_LABELS})

    setup_queue: asyncio.Queue | None = None
    last_seen: dict = {}
    last_scraped_data: dict = {}
    poll_task: asyncio.Task | None = None

    try:
        while True:
            data = await ws.receive_text()

            if data == "brief":
                if poll_task and not poll_task.done():
                    poll_task.cancel()
                    poll_task = None
                last_scraped_data = await _run_briefing(ws, last_seen)
                poll_task = asyncio.create_task(_polling_loop(ws, last_seen))

            elif data.startswith("ask:"):
                question = data[4:].strip()
                context = last_scraped_data or db.last_scraped() or {}
                if not context:
                    await ws.send_json({"type": "qa_chunk", "text": "No briefing data available yet, sir. Please run a briefing first."})
                else:
                    try:
                        async for chunk in answer_question(question, context):
                            await ws.send_json({"type": "qa_chunk", "text": chunk})
                    except Exception as e:
                        await ws.send_json({"type": "qa_chunk", "text": f"Apologies, sir — {e}"})
                await ws.send_json({"type": "qa_done"})

            elif data == "setup":
                setup_queue = asyncio.Queue()
                asyncio.create_task(_run_setup(ws, setup_queue))

            elif data == "setup_next" and setup_queue is not None:
                await setup_queue.put("next")

            elif data.startswith("retry:"):
                service_key = data.split(":", 1)[1]
                await _retry_service(ws, service_key)

            elif data.startswith("relogin:"):
                service_key = data.split(":", 1)[1]
                setup_queue = asyncio.Queue()
                asyncio.create_task(_run_relogin(ws, service_key, setup_queue))

    except WebSocketDisconnect:
        if poll_task and not poll_task.done():
            poll_task.cancel()
    finally:
        _connected_ws.discard(ws)


async def _run_briefing(ws: WebSocket, last_seen: dict) -> dict:
    async def status_cb(service: str, state: str):
        try:
            await ws.send_json({"type": "status", "service": service, "state": state})
        except Exception:
            pass

    try:
        await ws.send_json({"type": "phase", "phase": "scanning"})
    except Exception:
        return {}

    try:
        scraped_data = await run_all_scrapers(status_cb)
    except WebSocketDisconnect:
        return {}
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
        return {}

    for key in POLL_SERVICES:
        if key in scraped_data:
            last_seen[key] = scraped_data[key]
        mapped = {"gmail_1": "gmail_1", "gmail_2": "gmail_2"}.get(key, key)
        if mapped in scraped_data:
            last_seen[key] = scraped_data[mapped]

    await _send_results(ws, scraped_data)
    return scraped_data


async def _retry_service(ws: WebSocket, service_key: str):
    async def status_cb(service: str, state: str):
        await ws.send_json({"type": "status", "service": service, "state": state})

    await status_cb(service_key, "scanning")
    try:
        result = await scrape_one_service(service_key)
        if result:
            await ws.send_json({"type": "service_data", "service": service_key, "data": result})
            await ws.send_json({"type": "health_update", "services": {service_key: _health_payload(service_key, result)}})
            await status_cb(service_key, "done" if not result.get("error") and not (result.get("meta") or {}).get("stale") else "error")
    except Exception as e:
        await ws.send_json({"type": "error", "message": f"Retry failed: {e}"})
        await status_cb(service_key, "error")


async def _send_results(ws: WebSocket, scraped_data: dict):
    async def _send(payload: dict):
        try:
            await ws.send_json(payload)
        except Exception:
            raise WebSocketDisconnect(code=1000)

    await _send({"type": "health_update", "services": _health_index(scraped_data)})

    for key, svc_data in scraped_data.items():
        if svc_data:
            await _send({"type": "service_data", "service": key, "data": svc_data})

    now_ts = int(time.time())
    all_events = []
    for key in ["calendar_gmail1", "calendar_gmail2", "calendar_outlook"]:
        cal = scraped_data.get(key, {})
        meta = cal.get("meta") or {}
        if cal and (not cal.get("error") or meta.get("stale")):
            for ev in cal.get("events", []):
                all_events.append({**ev, "source": cal.get("service", key)})

    for ev in all_events:
        if not ev.get("start_ts") and ev.get("time"):
            ts = _time_str_to_ts(ev["time"])
            if ts:
                ev["start_ts"] = ts

    timed      = sorted([e for e in all_events if e.get("start_ts")], key=lambda e: e["start_ts"])
    time_only  = [e for e in all_events if not e.get("start_ts") and e.get("time")]
    title_only = [e for e in all_events if not e.get("start_ts") and not e.get("time") and e.get("title")]
    await _send({"type": "calendar_events", "events": timed + time_only + title_only})

    try:
        action_items = await generate_action_items(scraped_data)
        await _send({"type": "action_items", "items": action_items})
    except WebSocketDisconnect:
        raise
    except Exception as e:
        await _send({
            "type": "action_items",
            "items": [{
                "priority": "info",
                "item": "Action extraction unavailable",
                "source": "JARVIS",
                "from": "System",
                "subject": "Action-item validation",
                "detail": f"Action item extraction failed for this scan: {e}. The prose briefing is still available.",
            }],
        })

    await _send({"type": "phase", "phase": "briefing"})
    previous_data = db.last_scraped()
    briefing_chunks: list[str] = []
    try:
        async for chunk in generate_briefing(scraped_data, previous_data=previous_data):
            await _send({"type": "briefing_chunk", "text": chunk})
            briefing_chunks.append(chunk)
    except WebSocketDisconnect:
        raise
    except Exception as e:
        await _send({"type": "error", "message": f"Briefing generation failed: {e}"})
        return

    try:
        db.save_briefing("".join(briefing_chunks), scraped_data)
    except Exception:
        pass

    await _send({"type": "done"})


async def _run_relogin(ws: WebSocket, service_key: str, queue: asyncio.Queue):
    """Re-open browser for a single service so the user can log back in, then auto-retry."""
    profile = SERVICE_PROFILE_MAP.get(service_key)
    if not profile:
        await ws.send_json({"type": "error", "message": f"Unknown service: {service_key}"})
        return

    async def status_cb(svc: str, state: str):
        await ws.send_json({"type": "setup_status", "profile": svc, "state": state})

    await ws.send_json({"type": "phase", "phase": "relogin", "service": service_key})
    try:
        await relogin_one_service(profile, queue, status_cb)
    except Exception as e:
        await ws.send_json({"type": "error", "message": str(e)})
        return

    await ws.send_json({"type": "relogin_done", "service": service_key})
    # Auto-retry the scrape now that the session is refreshed
    await _retry_service(ws, service_key)


async def _run_setup(ws: WebSocket, queue: asyncio.Queue):
    async def status_cb(profile: str, state: str):
        await ws.send_json({"type": "setup_status", "profile": profile, "state": state})

    await ws.send_json({"type": "phase", "phase": "setup"})
    try:
        await setup_logins(status_cb, queue)
    except Exception as e:
        await ws.send_json({"type": "error", "message": str(e)})
        return
    await ws.send_json({"type": "setup_done"})


@app.on_event("startup")
async def _start_scheduler():
    if _BRIEFING_TIMES:
        asyncio.create_task(_auto_brief_scheduler())


async def _auto_brief_scheduler():
    last_fired = ""
    while True:
        await asyncio.sleep(30)
        now_str = datetime.datetime.now().strftime("%H:%M")
        if now_str in _BRIEFING_TIMES and now_str != last_fired:
            last_fired = now_str
            for ws in list(_connected_ws):
                try:
                    await _run_briefing(ws, {})
                except Exception:
                    pass


@app.get("/history")
async def history_endpoint(limit: int = 20):
    return db.history(limit)


@app.get("/health")
async def health_endpoint():
    snapshots = db.service_snapshot_index()
    return _health_index(snapshots)


_TTS_VOICE = "en-GB-RyanNeural"
_TTS_RATES = {
    "greeting": "-10%",
    "briefing": "-14%",
    "critical": "-20%",
    "status":   "-5%",
    "detail":   "-12%",
}

@app.get("/tts")
async def tts_endpoint(text: str = Query(...), context: str = Query("briefing")):
    rate = _TTS_RATES.get(context, "-14%")

    async def generate():
        try:
            communicate = edge_tts.Communicate(text, voice=_TTS_VOICE, rate=rate)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
        except Exception:
            return

    return StreamingResponse(generate(), media_type="audio/mpeg")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
