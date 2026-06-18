import json
import sqlite3
from datetime import datetime
from pathlib import Path

_DB = Path.home() / ".jarvis" / "history.db"


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB), check_same_thread=False)
    c.execute("""
        CREATE TABLE IF NOT EXISTS briefings (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts   INTEGER NOT NULL,
            text TEXT    NOT NULL,
            data TEXT    NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS service_snapshots (
            service_key TEXT PRIMARY KEY,
            ts          INTEGER NOT NULL,
            data        TEXT    NOT NULL
        )
    """)
    c.commit()
    return c


def save_briefing(text: str, data: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO briefings(ts, text, data) VALUES(?, ?, ?)",
            (int(datetime.now().timestamp()), text, json.dumps(data, default=str)),
        )


def last_scraped() -> dict | None:
    row = _conn().execute(
        "SELECT data FROM briefings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return json.loads(row[0]) if row else None


def history(limit: int = 20) -> list[dict]:
    rows = _conn().execute(
        "SELECT id, ts, text FROM briefings ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [{"id": r[0], "ts": r[1], "text": r[2][:600]} for r in rows]


def save_service_snapshot(service_key: str, data: dict) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT INTO service_snapshots(service_key, ts, data)
            VALUES(?, ?, ?)
            ON CONFLICT(service_key) DO UPDATE SET
              ts = excluded.ts,
              data = excluded.data
            """,
            (service_key, int(datetime.now().timestamp()), json.dumps(data, default=str)),
        )


def last_service_snapshot(service_key: str) -> dict | None:
    row = _conn().execute(
        "SELECT ts, data FROM service_snapshots WHERE service_key = ?",
        (service_key,),
    ).fetchone()
    if not row:
        return None
    data = json.loads(row[1])
    data.setdefault("meta", {})
    data["meta"]["last_success_ts"] = row[0]
    return data


def service_snapshot_index() -> dict:
    rows = _conn().execute(
        "SELECT service_key, ts, data FROM service_snapshots ORDER BY service_key"
    ).fetchall()
    out = {}
    for key, ts, data_text in rows:
        data = json.loads(data_text)
        data.setdefault("meta", {})
        data["meta"]["last_success_ts"] = ts
        out[key] = data
    return out
