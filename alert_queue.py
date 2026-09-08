# -*- coding: utf-8 -*-
"""Durable delivery queue for deterministic market alerts.

The quote watcher must not treat an event as delivered merely because it was
detected.  We persist it first, retry failed sends with bounded backoff, and
only let callers mark their daily de-duplication state after the Weixin bridge
accepts the message.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Callable


_DEFAULT_DB = Path(__file__).resolve().parent / "data" / "alert_queue.db"
_MAX_ATTEMPTS = 8


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: Path | str = _DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_queue (
            event_key TEXT PRIMARY KEY,
            priority INTEGER NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'retrying', 'sending', 'delivered', 'dead')),
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            lease_until TEXT,
            last_error TEXT
        )
    """)
    return conn


def enqueue(event_key: str, message: str, priority: int, *, db_path: Path | str = _DEFAULT_DB,
            now: dt.datetime | None = None) -> str:
    """Persist one alert.  Return ``queued`` or the existing delivery status.

    ``event_key`` is stable for one symbol/event/day, so a cron retry cannot
    create duplicate notifications.
    """
    now_text = _iso(now or _utcnow())
    with _connect(db_path) as conn:
        row = conn.execute("SELECT status FROM alert_queue WHERE event_key=?", (event_key,)).fetchone()
        if row:
            return str(row["status"])
        conn.execute(
            """INSERT INTO alert_queue
               (event_key, priority, message, status, attempts, next_attempt_at, created_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?)""",
            (event_key, priority, message, now_text, now_text),
        )
    return "queued"


def flush(sender: Callable[[str], bool], *, db_path: Path | str = _DEFAULT_DB,
          limit: int = 10, now: dt.datetime | None = None) -> dict:
    """Attempt due alerts and return verifiable delivery counts.

    A sender returning ``True`` means the delivery bridge accepted the message;
    this is deliberately weaker than claiming the handset displayed it.
    """
    now = now or _utcnow()
    now_text = _iso(now)
    delivered = failed = 0
    with _connect(db_path) as conn:
        # Claim before making external calls.  Without this short lease, two
        # overlapping cron processes can both select and send the same alert.
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT event_key, message, attempts FROM alert_queue
               WHERE (status IN ('pending', 'retrying') AND next_attempt_at <= ?)
                  OR (status='sending' AND lease_until <= ?)
               ORDER BY priority ASC, created_at ASC LIMIT ?""",
            (now_text, now_text, limit),
        ).fetchall()
        lease_until = _iso(now + dt.timedelta(minutes=2))
        for row in rows:
            conn.execute(
                "UPDATE alert_queue SET status='sending', lease_until=? WHERE event_key=?",
                (lease_until, str(row["event_key"])),
            )
        conn.commit()
        for row in rows:
            key, message, attempts = str(row["event_key"]), str(row["message"]), int(row["attempts"])
            try:
                accepted = bool(sender(message))
            except Exception as exc:  # sender implementations are external I/O
                accepted = False
                error = f"{type(exc).__name__}: {exc}"[:500]
            else:
                error = "delivery bridge rejected message" if not accepted else None

            if accepted:
                conn.execute(
                    """UPDATE alert_queue SET status='delivered', delivered_at=?, lease_until=NULL,
                       last_error=NULL WHERE event_key=?""",
                    (now_text, key),
                )
                delivered += 1
                continue

            next_attempts = attempts + 1
            if next_attempts >= _MAX_ATTEMPTS:
                conn.execute(
                    """UPDATE alert_queue SET status='dead', attempts=?, lease_until=NULL,
                       last_error=? WHERE event_key=?""",
                    (next_attempts, error, key),
                )
            else:
                # 1, 2, 4 ... minutes, capped at 30 minutes.  A transient
                # gateway restart therefore cannot erase a stop-loss alert.
                delay_minutes = min(2 ** (next_attempts - 1), 30)
                due = _iso(now + dt.timedelta(minutes=delay_minutes))
                conn.execute(
                    """UPDATE alert_queue SET status='retrying', attempts=?, next_attempt_at=?, lease_until=NULL, last_error=?
                       WHERE event_key=?""",
                    (next_attempts, due, error, key),
                )
            failed += 1
    return {"delivered": delivered, "failed": failed, "attempted": len(rows)}


def status(event_key: str, *, db_path: Path | str = _DEFAULT_DB) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT status FROM alert_queue WHERE event_key=?", (event_key,)).fetchone()
    return str(row["status"]) if row else None
