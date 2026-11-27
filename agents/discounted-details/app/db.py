"""SQLite operational state (PRD §10.3). The Google Sheet is the CRM of record; this holds what the runtime needs."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY, source TEXT, processed_at TEXT);
CREATE TABLE IF NOT EXISTS threads (
  thread_id TEXT PRIMARY KEY, channel TEXT, sender_id TEXT, customer_id TEXT,
  last_inbound_at TEXT, paused_until TEXT, last_polled_at TEXT,
  quote_checkin_sent INTEGER DEFAULT 0, last_quote_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS llm_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, turn_id TEXT,
  role TEXT, content_json TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_hist_thread ON llm_history(thread_id, id);
CREATE TABLE IF NOT EXISTS sent_messages (message_id TEXT PRIMARY KEY, thread_id TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS approvals_mirror (approval_id TEXT PRIMARY KEY, thread_id TEXT, type TEXT, status TEXT,
  payload_json TEXT, created_at TEXT, last_ping_at TEXT, telegram_message_id INTEGER);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(v: str | None) -> datetime | None:
    return clock.parse_iso(v) if v else None


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args).fetchall())

    # --- dedupe ---
    def mark_event_processed(self, event_id: str, source: str = "") -> bool:
        """True if this is the first time we've seen the event."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed_events VALUES (?,?,?)",
                (event_id, source, clock.now().isoformat()),
            )
            return cur.rowcount == 1

    def is_event_processed(self, event_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)))

    # --- threads ---
    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM threads WHERE thread_id=?", (thread_id,))
        if not rows:
            return None
        r = dict(rows[0])
        for k in ("last_inbound_at", "paused_until", "last_polled_at", "last_quote_at", "updated_at"):
            r[k] = _dt(r[k])
        r["quote_checkin_sent"] = bool(r["quote_checkin_sent"])
        return r

    def upsert_thread(self, thread_id: str, **fields: Any) -> dict[str, Any]:
        existing = self.get_thread(thread_id) or {
            "thread_id": thread_id, "channel": None, "sender_id": None, "customer_id": None,
            "last_inbound_at": None, "paused_until": None, "last_polled_at": None,
            "quote_checkin_sent": False, "last_quote_at": None,
        }
        existing.update(fields)
        existing["updated_at"] = clock.now()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
                (thread_id, existing["channel"], existing["sender_id"], existing["customer_id"],
                 _iso(existing["last_inbound_at"]), _iso(existing["paused_until"]),
                 _iso(existing["last_polled_at"]), int(bool(existing["quote_checkin_sent"])),
                 _iso(existing["last_quote_at"]), _iso(existing["updated_at"])),
            )
        return existing

    def list_threads(self) -> list[dict[str, Any]]:
        return [t for r in self._q("SELECT thread_id FROM threads") if (t := self.get_thread(r["thread_id"]))]

    # --- LLM history (append-only) ---
    def append_history(self, thread_id: str, turn_id: str, role: str, content: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO llm_history(thread_id,turn_id,role,content_json,created_at) VALUES (?,?,?,?,?)",
                (thread_id, turn_id, role, json.dumps(content), clock.now().isoformat()),
            )

    def load_history(self, thread_id: str, limit_turns: int = 30) -> list[dict[str, Any]]:
        """Messages from the most recent `limit_turns` turns, oldest first (whole turns only)."""
        turns = self._q(
            "SELECT turn_id, MIN(id) m FROM llm_history WHERE thread_id=? GROUP BY turn_id ORDER BY m DESC LIMIT ?",
            (thread_id, limit_turns),
        )
        if not turns:
            return []
        first_id = min(r["m"] for r in turns)
        rows = self._q("SELECT role, content_json FROM llm_history WHERE thread_id=? AND id>=? ORDER BY id",
                       (thread_id, first_id))
        return [{"role": r["role"], "content": json.loads(r["content_json"])} for r in rows]

    def prune_history(self, older_than: timedelta) -> int:
        cutoff = (clock.now() - older_than).isoformat()
        with self._lock:
            return self._conn.execute("DELETE FROM llm_history WHERE created_at < ?", (cutoff,)).rowcount

    # --- messages Dex sent (owner-takeover detection, FR-M9) ---
    def record_sent(self, message_id: str, thread_id: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?,?,?)",
                               (message_id, thread_id, clock.now().isoformat()))

    def was_sent_by_dex(self, message_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM sent_messages WHERE message_id=?", (message_id,)))

    # --- approvals mirror ---
    def save_approval(self, approval_id: str, thread_id: str | None, type_: str, status: str,
                      payload: dict[str, Any], telegram_message_id: int | None = None) -> None:
        prev = self.get_approval(approval_id)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO approvals_mirror VALUES (?,?,?,?,?,?,?,?)",
                (approval_id, thread_id, type_, status, json.dumps(payload),
                 prev["created_at"].isoformat() if prev else clock.now().isoformat(),
                 _iso(prev["last_ping_at"]) if prev else None,
                 telegram_message_id if telegram_message_id is not None else (prev or {}).get("telegram_message_id")),
            )

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM approvals_mirror WHERE approval_id=?", (approval_id,))
        if not rows:
            return None
        r = dict(rows[0])
        r["payload"] = json.loads(r.pop("payload_json"))
        r["created_at"] = _dt(r["created_at"])
        r["last_ping_at"] = _dt(r["last_ping_at"])
        return r

    def list_approvals(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT approval_id FROM approvals_mirror" + (" WHERE status=?" if status else "") + " ORDER BY created_at"
        return [a for r in self._q(sql, (status,) if status else ()) if (a := self.get_approval(r["approval_id"]))]

    def set_approval_ping(self, approval_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE approvals_mirror SET last_ping_at=? WHERE approval_id=?",
                               (clock.now().isoformat(), approval_id))

    def next_counter(self, name: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (name,)).fetchone()
            n = int(row["value"]) + 1 if row else 1
            self._conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (name, str(n), clock.now().isoformat()))
            return n

    # --- kv ---
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                               (key, json.dumps(value), clock.now().isoformat()))

    def prune_events(self, older_than: timedelta) -> int:
        cutoff = (clock.now() - older_than).isoformat()
        with self._lock:
            return self._conn.execute("DELETE FROM processed_events WHERE processed_at < ?", (cutoff,)).rowcount
