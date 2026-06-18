import os
import json as _json
from datetime import datetime
from typing import AsyncIterator
from groq import AsyncGroq

client = AsyncGroq()

_USER_NAME = os.getenv("USER_NAME", "")

_IDENTITY_CLAUSE = (
    f"\n\nCRITICAL — USER IDENTITY: The person you are briefing is named {_USER_NAME}. "
    f"When you encounter '{_USER_NAME}' anywhere in the data — in meeting titles, email threads, "
    f"calendar invites, or messages — it refers to the user themselves, not an external contact. "
    f"NEVER say 'your meeting with {_USER_NAME}' or 'an email from {_USER_NAME}'. "
    f"Say 'your meeting' or 'your email' instead. Treat {_USER_NAME} as 'you/sir'."
    if _USER_NAME else ""
)

SYSTEM_PROMPT = """You are J.A.R.V.I.S. — a personal AI assistant who has personally reviewed every message, email, and calendar entry before this briefing. You speak like a sharp human PA who has actually read everything — not like a system listing data fields.

Core rules:
- Always address the user as "sir"
- Reference people by their ACTUAL NAME from the data (never say "someone" or "a sender")
- PARAPHRASE what people actually said or asked — never say "there are X unread messages, please check"
- CROSS-SERVICE PATTERNS: If the same person appears across multiple channels (e.g. emailed you AND sent a Slack DM AND is in a meeting), flag this explicitly — it signals urgency. Say something like "Sarah has reached out across three channels — email, Slack, and your 2pm — she appears to need you urgently."
- Connect related items: if a Slack message and a meeting are about the same project, say so
- Calculate urgency from the current time vs event times — if a meeting is in 15 minutes, say "in 15 minutes"
- Never list raw counts ("3 unread") — synthesise the content instead
- Speak in flowing, natural sentences. No bullet points. No structured lists.

Briefing structure (in prose, no headers):
1. Open with a sharp one-sentence read on the day's most urgent item
2. Walk through critical items — name who, what they need, by when. Call out any cross-service patterns first.
3. Cover upcoming meetings with precise timing ("your 4:30 call with X starts in 12 minutes")
4. Weave in important but non-urgent items naturally — include any unread Outlook emails and Teams messages by name
5. Close with one dry, confident Jarvis-style line

IMPORTANT: If there are unread emails from Outlook or messages from Teams, you MUST mention them specifically — name the sender and topic. Do not skip any source that has data.

Under 350 words. Sound like you personally reviewed everything — because you did.""" + _IDENTITY_CLAUSE


def _get_time_greeting() -> str:
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Good morning, sir."
    elif 12 <= hour < 17:
        return "Good afternoon, sir."
    elif 17 <= hour < 21:
        return "Good evening, sir."
    else:
        return "Good night, sir."


def _build_prompt(data: dict) -> str:
    now = datetime.now().strftime("%A, %B %d %Y — %I:%M %p")
    lines = [f"Current time: {now}"]
    if _USER_NAME:
        lines.append(f"User identity: {_USER_NAME} (this person is the one being briefed — treat their name in the data as 'the user', not an external contact)")
    lines.append("")

    for key, service_data in data.items():
        if not service_data:
            continue
        name = service_data.get("service", key)
        error = service_data.get("error")
        meta = service_data.get("meta") or {}
        if meta.get("stale"):
            last = meta.get("last_success_ts")
            last_text = datetime.fromtimestamp(last).strftime("%I:%M %p") if last else "an earlier scan"
            lines.append(f"[{name}]: CURRENT SCAN FAILED — using stale data from {last_text}. Error: {error or 'unknown'}")

        if error == "not_logged_in" and not meta.get("stale"):
            lines.append(f"[{name}]: Session not authenticated — skipped.")
            continue
        if error and not meta.get("stale"):
            lines.append(f"[{name}]: Error during scan — {error}")
            continue

        emails   = service_data.get("emails", [])
        chats    = service_data.get("chats", [])
        messages = service_data.get("messages", [])
        events   = service_data.get("events", [])

        if emails:
            lines.append(f"[{name} — Unread Emails ({len(emails)}):]")
            for e in emails:
                snippet = e.get("snippet", "")[:250]
                lines.append(f"  From: {e['sender']}")
                lines.append(f"  Subject: {e['subject']}")
                if snippet:
                    lines.append(f"  Preview: {snippet}")
                lines.append("")

        if chats:
            lines.append(f"[{name} — Chat Messages ({len(chats)}):]")
            for c in chats:
                tag = " [DM]" if c.get("type") == "dm" else " [Space]"
                preview = c.get("preview", "")[:200]
                lines.append(f"  From: {c['from']}{tag}")
                if preview:
                    lines.append(f"  Message: {preview}")
                lines.append("")

        if messages:
            lines.append(f"[{name} — Unread Messages ({len(messages)}):]")
            for m in messages:
                ch      = m.get("channel") or m.get("chat", "Unknown")
                count   = m.get("unread", "?")
                preview = m.get("preview", "")
                recent  = m.get("recent_messages", [])
                tag = ""
                if m.get("is_mention"):
                    tag = " [@mention]"
                elif m.get("is_dm"):
                    tag = " [DM]"
                lines.append(f"  {ch}{tag}: {count} unread")
                # Show actual message content when available
                if recent:
                    for rm in recent[-5:]:
                        sender = rm.get("sender", "")
                        text   = rm.get("text", "")[:220]
                        if sender and text:
                            lines.append(f"    → {sender}: {text}")
                        elif text:
                            lines.append(f"    → {text}")
                elif preview:
                    lines.append(f"    → {preview[:220]}")
                lines.append("")

        if events:
            lines.append(f"[{name} — Today's Schedule ({len(events)}):]")
            for ev in events:
                status = ev.get("status", "upcoming")
                t      = ev.get("time", "")
                title  = ev["title"]
                lines.append(f"  {t} — {title} [{status}]")
            lines.append("")

    return "\n".join(lines)


def _extract_json_array(text: str) -> list | None:
    start, end = text.find("["), text.rfind("]") + 1
    if start < 0 or end <= start:
        return None
    try:
        parsed = _json.loads(text[start:end])
    except Exception:
        return None
    return parsed if isinstance(parsed, list) else None


def _validate_action_items(items: list) -> list:
    out = []
    allowed = {"critical", "important", "info"}
    for raw in items[:8]:
        if not isinstance(raw, dict):
            continue
        priority = str(raw.get("priority") or "info").lower()
        if priority not in allowed:
            priority = "info"
        item = str(raw.get("item") or "").strip()
        detail = str(raw.get("detail") or "").strip()
        if not item or not detail:
            continue
        if _is_system_health_action(raw, item, detail):
            continue
        if _is_routine_calendar_action(raw, item, detail):
            continue
        out.append({
            "priority": priority,
            "item": item[:140],
            "source": str(raw.get("source") or "Unknown").strip()[:80],
            "from": str(raw.get("from") or "").strip()[:120],
            "subject": str(raw.get("subject") or "").strip()[:160],
            "detail": detail[:600],
        })
    return out


def _is_system_health_action(raw: dict, item: str, detail: str) -> bool:
    """Filter scraper/runtime failures out of user-facing action items."""
    source = str(raw.get("source") or "").strip().lower()
    sender = str(raw.get("from") or "").strip().lower()
    subject = str(raw.get("subject") or "").strip().lower()
    combined = " ".join([item, detail, source, sender, subject]).lower()

    health_terms = [
        "error during scan", "scan failed", "scraper", "diagnostic",
        "not_logged_in", "not logged in", "unavailable", "auth wall",
        "invalid or unexpected token", "fix dentsu calendar error",
        "fix calendar error", "fix outlook calendar error",
    ]
    system_sources = {"jarvis", "system", "j.a.r.v.i.s."}
    if any(term in combined for term in health_terms):
        if not sender or sender in system_sources or "error" in subject or source in system_sources:
            return True
        if source.endswith("calendar") and ("scan" in combined or "error" in combined):
            return True
    return False


def _is_routine_calendar_action(raw: dict, item: str, detail: str) -> bool:
    """Drop normal meetings from action items; they belong in the schedule."""
    source = str(raw.get("source") or "").strip().lower()
    subject = str(raw.get("subject") or "").strip().lower()
    combined = " ".join([item, detail, subject]).lower()
    if "calendar" not in source:
        return False

    routine_starts = (
        "join ", "attend ", "review ", "go to ", "dial into ",
        "show up", "prepare for ", "remember ",
    )
    routine_meeting_terms = (
        "standup", "stand up", "meeting", "call", "event", "calendar",
        "starts", "starting", "scheduled", "today", "tonight",
    )
    real_action_terms = (
        "asked", "request", "requested", "approve", "reply", "respond",
        "send", "submit", "complete", "follow up", "deadline", "due",
        "blocker", "decision", "review the document", "share",
    )
    starts_like_routine = item.lower().startswith(routine_starts)
    looks_like_meeting = any(term in combined for term in routine_meeting_terms)
    has_real_action = any(term in combined for term in real_action_terms)
    return (starts_like_routine or looks_like_meeting) and not has_real_action


def _action_warning(reason: str) -> list:
    return [{
        "priority": "info",
        "item": "Action extraction needs review",
        "source": "JARVIS",
        "from": "System",
        "subject": "LLM JSON validation",
        "detail": f"J.A.R.V.I.S. could not validate structured action-item JSON for this scan ({reason}). The briefing prose still used the scanned data.",
    }]


async def generate_action_items(data: dict) -> list:
    """Return a structured list of action items from scraped data."""
    prompt = _build_prompt(data)
    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=1200,
        stream=False,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are extracting action items from real communication data that was personally reviewed. "
                    "Return ONLY a valid JSON array — no markdown, no explanation. "
                    'Each element: {"priority": "critical"|"important"|"info", '
                    '"item": "specific action using real names, max 12 words (e.g. \'Reply to John re: API deadline extension\')", '
                    '"source": "the account label from the section header (e.g. \'Successive Digital Gmail\', \'Slack\', \'Teams\') — never a sender email domain", '
                    '"from": "exact sender name from the data, never empty if a sender is shown", '
                    '"subject": "email subject line or message topic — use the actual subject/topic from the data", '
                    '"detail": "Write 2-3 natural sentences as a human PA would: who sent it, what they specifically said or asked (quote or paraphrase the actual message), and what action is needed. Never write generic text like \'check the channel\' or \'review messages\'. Use real names and real content."}. '
                    "Max 8 items. Critical = urgent/time-sensitive. Important = needs action today. Info = worth knowing. "
                    "RULES: (1) The 'item' field must name the person and the specific ask. (2) The 'detail' field must paraphrase what was actually said — never generic. (3) 'source' must match the section header label exactly. "
                    "Do not create action items for scraper failures, unavailable services, stale-data warnings, authentication errors, diagnostics, or system/runtime errors; those are service health, not user work. "
                    "Do not create action items for ordinary calendar meetings or events like 'join standup', 'attend event', or 'review meeting'; those belong only in the schedule unless the calendar details contain a concrete ask, deliverable, deadline, approval, or follow-up."
                ),
            },
            {"role": "user", "content": f"Extract action items from this data:\n\n{prompt}"},
        ],
    )
    text = response.choices[0].message.content.strip()
    print(f"[action-items] raw LLM response ({len(text)} chars): {text[:300]}", flush=True)
    parsed = _extract_json_array(text)
    items = _validate_action_items(parsed or [])
    if items or parsed == []:
        print(f"[action-items] parsed {len(items)} valid items", flush=True)
        return items

    print("[action-items] invalid JSON/schema, attempting one repair", flush=True)
    try:
        repair = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1200,
            stream=False,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Repair the user's content into ONLY a valid JSON array. "
                        'Each object must have: priority, item, source, from, subject, detail. '
                        "No markdown, no explanation."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        repaired_text = repair.choices[0].message.content.strip()
        repaired = _extract_json_array(repaired_text)
        items = _validate_action_items(repaired or [])
        if items or repaired == []:
            print(f"[action-items] repaired {len(items)} valid items", flush=True)
            return items
    except Exception as e:
        print(f"[action-items] repair failed: {e}", flush=True)

    return _action_warning("invalid_or_empty_response")


def _build_delta_summary(current: dict, previous: dict) -> str:
    lines = []
    for key in current:
        curr = current.get(key) or {}
        prev = previous.get(key) or {}
        if curr.get("error") or prev.get("error"):
            continue
        name = curr.get("service", key)

        curr_emails = {f"{e.get('sender','')}|{e.get('subject','')}" for e in curr.get("emails", [])}
        prev_emails = {f"{e.get('sender','')}|{e.get('subject','')}" for e in prev.get("emails", [])}
        new_n = len(curr_emails - prev_emails)
        if new_n:
            lines.append(f"{name}: {new_n} new email(s) since last briefing")

        curr_msgs = len(curr.get("messages", [])) + len(curr.get("chats", []))
        prev_msgs = len(prev.get("messages", [])) + len(prev.get("chats", []))
        if curr_msgs > prev_msgs:
            lines.append(f"{name}: {curr_msgs - prev_msgs} new message thread(s)")

    return "\n".join(lines)


async def generate_briefing(data: dict, previous_data: dict | None = None) -> AsyncIterator[str]:
    """Yield briefing text chunks as they stream from Groq."""
    greeting = _get_time_greeting()
    prompt = _build_prompt(data)

    if previous_data:
        delta = _build_delta_summary(data, previous_data)
        if delta:
            prompt += f"\n\nNEW SINCE LAST BRIEFING (lead with these changes):\n{delta}"

    yield greeting + " "

    stream = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=800,
        stream=True,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Here is my current data. Please deliver my briefing.\n\n{prompt}"},
        ],
    )

    async for chunk in stream:
        text = chunk.choices[0].delta.content
        if text:
            yield text


async def answer_question(question: str, data: dict) -> AsyncIterator[str]:
    """Stream a conversational follow-up answer given the briefing context."""
    prompt = _build_prompt(data)
    stream = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=350,
        stream=True,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT + "\n\nYou are now answering a follow-up question. Be concise — 2 to 4 sentences. Use the briefing data to give a specific, grounded answer.",
            },
            {"role": "user", "content": f"Briefing data:\n\n{prompt}"},
            {"role": "user", "content": question},
        ],
    )
    async for chunk in stream:
        text = chunk.choices[0].delta.content
        if text:
            yield text


def get_greeting() -> str:
    return _get_time_greeting()
