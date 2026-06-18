import asyncio
import html
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import db

JARVIS_DIR = Path.home() / ".jarvis"
DIAG_DIR = JARVIS_DIR / "diagnostics"
DEFAULT_SERVICE_TIMEOUT = int(os.getenv("SCRAPER_TIMEOUT_SECONDS", "90"))
DEFAULT_RETRIES = int(os.getenv("SCRAPER_RETRIES", "1"))

RUN_ID = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
_PROFILE_LOCKS: dict[str, asyncio.Lock] = {}


def now_ts() -> int:
    return int(time.time())


def profile_is_locked(profile: str) -> bool:
    return _PROFILE_LOCKS.setdefault(profile, asyncio.Lock()).locked()


@asynccontextmanager
async def profile_lock(profile: str, wait: bool = True):
    lock = _PROFILE_LOCKS.setdefault(profile, asyncio.Lock())
    if lock.locked() and not wait:
        yield False
        return
    await lock.acquire()
    try:
        yield True
    finally:
        lock.release()


def item_count(data: dict) -> int:
    return (
        len(data.get("emails") or [])
        + len(data.get("chats") or [])
        + len(data.get("messages") or [])
        + len(data.get("events") or [])
    )


def classify_error(exc_or_text: Any) -> str:
    text = str(exc_or_text or "").lower()
    if "not_logged_in" in text or "sign in" in text or "session" in text:
        return "auth"
    if "timeout" in text:
        return "timeout"
    if "profile_busy" in text or "lock" in text:
        return "profile_busy"
    if "selector" in text or "locator" in text:
        return "selector"
    return "unknown"


def confidence_for(data: dict) -> str:
    if data.get("error"):
        return "none"
    if item_count(data) > 0:
        return "high"
    return "empty"


def service_meta(
    *,
    service_key: str,
    profile: str,
    started_at: float,
    status: str,
    error: str | None = None,
    diagnostic_path: str | None = None,
    stale: bool = False,
    last_success_ts: int | None = None,
) -> dict:
    meta = {
        "service_key": service_key,
        "profile": profile,
        "status": status,
        "auth_status": "needs_login" if error == "not_logged_in" else "ok",
        "error_code": classify_error(error) if error else None,
        "duration_ms": int((time.perf_counter() - started_at) * 1000),
        "item_count": 0,
        "confidence": "none",
        "stale": stale,
        "last_success_ts": last_success_ts,
        "diagnostic_path": diagnostic_path,
        "run_id": RUN_ID,
    }
    return meta


def attach_success_meta(data: dict, service_key: str, profile: str, started_at: float) -> dict:
    out = dict(data or {})
    out.setdefault("error", None)
    out["meta"] = service_meta(
        service_key=service_key,
        profile=profile,
        started_at=started_at,
        status="ok",
        last_success_ts=now_ts(),
    )
    out["meta"]["item_count"] = item_count(out)
    out["meta"]["confidence"] = confidence_for(out)
    return out


def attach_failure_meta(
    data: dict | None,
    service_key: str,
    profile: str,
    started_at: float,
    error: Any,
    diagnostic_path: str | None = None,
) -> dict:
    out = dict(data or {})
    out["error"] = out.get("error") or str(error)
    out["meta"] = service_meta(
        service_key=service_key,
        profile=profile,
        started_at=started_at,
        status="error",
        error=out["error"],
        diagnostic_path=diagnostic_path,
    )
    out["meta"]["item_count"] = item_count(out)
    out["meta"]["confidence"] = confidence_for(out)
    return out


def with_stale_fallback(service_key: str, profile: str, failure: dict, started_at: float) -> dict:
    cached = db.last_service_snapshot(service_key)
    if not cached:
        return failure
    out = dict(cached)
    out["error"] = failure.get("error")
    out.setdefault("meta", {})
    out["meta"].update(
        service_meta(
            service_key=service_key,
            profile=profile,
            started_at=started_at,
            status="stale",
            error=failure.get("error"),
            diagnostic_path=(failure.get("meta") or {}).get("diagnostic_path"),
            stale=True,
            last_success_ts=(cached.get("meta") or {}).get("last_success_ts"),
        )
    )
    out["meta"]["item_count"] = item_count(out)
    out["meta"]["confidence"] = "stale"
    return out


def persist_if_fresh(service_key: str, data: dict) -> None:
    meta = data.get("meta") or {}
    if data.get("error") or meta.get("stale"):
        return
    db.save_service_snapshot(service_key, data)


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_") or "service"


def _redact(text: str) -> str:
    text = re.sub(r"[\w.\-+]+@[\w.\-]+\.\w+", "[email]", text)
    text = re.sub(r"\b\d{6,}\b", "[number]", text)
    return text[:4000]


async def capture_page_diagnostic(page, service_key: str, reason: Any, extra: dict | None = None) -> str | None:
    service_dir = DIAG_DIR / RUN_ID / _safe_name(service_key)
    service_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "service_key": service_key,
        "reason": str(reason),
        "ts": now_ts(),
        "extra": extra or {},
    }
    try:
        payload["url"] = page.url
    except Exception:
        payload["url"] = ""
    try:
        payload["title"] = await page.title()
    except Exception:
        payload["title"] = ""
    try:
        body = await page.inner_text("body", timeout=2000)
        payload["body_preview"] = _redact(body)
    except Exception as exc:
        payload["body_error"] = str(exc)
    try:
        payload["selector_counts"] = await page.evaluate(
            """() => ({
                links: document.querySelectorAll('a').length,
                buttons: document.querySelectorAll('button,[role="button"]').length,
                inputs: document.querySelectorAll('input,textarea').length,
                unreadTextNodes: (document.body.innerText.match(/unread/ig) || []).length,
                ariaLabels: document.querySelectorAll('[aria-label]').length,
                dataTid: document.querySelectorAll('[data-tid]').length,
                dataEventId: document.querySelectorAll('[data-eventid]').length
            })"""
        )
    except Exception as exc:
        payload["selector_counts_error"] = str(exc)
    try:
        await page.screenshot(path=str(service_dir / "page.png"), full_page=True, timeout=5000)
        payload["screenshot"] = str(service_dir / "page.png")
    except Exception as exc:
        payload["screenshot_error"] = str(exc)

    (service_dir / "diagnostic.json").write_text(json.dumps(payload, indent=2, default=str))
    (service_dir / "body.html").write_text(html.escape(payload.get("body_preview", "")))
    return str(service_dir)
