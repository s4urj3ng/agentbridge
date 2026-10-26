"""Durable, idempotent event queue on SQLite."""
from __future__ import annotations

import sqlite3

from ..db import jdump, now_iso, tx

TL_EVENTS = {"GOAL_SUBMITTED", "TASK_READY_FOR_REVIEW", "TASK_BLOCKED", "VALIDATION_EXHAUSTED",
             "REVIEW_COMPLETED", "MILESTONE_READY", "HUMAN_DECISION", "GITHUB_FEEDBACK"}
SYSTEM_EVENTS = {"APPROVAL_RESOLVED"}


def emit(conn: sqlite3.Connection, type_: str, *, project_id: int | None, milestone_id: int | None = None,
         task_id: int | None = None, payload: dict | None = None, dedupe_key: str | None = None) -> int | None:
    """Insert an event. With a dedupe_key, a duplicate is silently ignored (returns None)."""
    assert type_ in TL_EVENTS | SYSTEM_EVENTS, type_
    cur = conn.execute(
        "INSERT OR IGNORE INTO events (project_id, milestone_id, task_id, type, payload_json, dedupe_key, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (project_id, milestone_id, task_id, type_, jdump(payload or {}), dedupe_key, now_iso()))
    return cur.lastrowid if cur.rowcount else None


def claim_next(conn: sqlite3.Connection, worker_id: str, *, exclude_projects: set[int],
               types: set[str]) -> sqlite3.Row | None:
    """Atomically claim the oldest pending event (per-project ordering preserved)."""
    with tx(conn):
        now = now_iso()
        candidates = conn.execute(
            "SELECT * FROM events WHERE status IN ('PENDING','DEFERRED') AND (not_before IS NULL OR not_before <= ?) "
            "ORDER BY id LIMIT 50", (now,)).fetchall()
        for ev in candidates:
            if ev["project_id"] in exclude_projects or ev["type"] not in types:
                continue
            # keep per-project order: skip if an older event of the project is in flight
            older = conn.execute("SELECT 1 FROM events WHERE project_id IS ? AND id < ? AND status='PROCESSING'",
                                 (ev["project_id"], ev["id"])).fetchone()
            if older:
                continue
            cur = conn.execute("UPDATE events SET status='PROCESSING', claimed_by=?, attempts=attempts+1 "
                               "WHERE id=? AND status IN ('PENDING','DEFERRED')", (worker_id, ev["id"]))
            if cur.rowcount == 1:
                return conn.execute("SELECT * FROM events WHERE id=?", (ev["id"],)).fetchone()
    return None


def finish(conn: sqlite3.Connection, event_id: int, status: str = "DONE", error: str | None = None) -> None:
    conn.execute("UPDATE events SET status=?, error=?, processed_at=? WHERE id=?",
                 (status, error, now_iso(), event_id))


def defer(conn: sqlite3.Connection, event_id: int, seconds: float, reason: str, refund_attempt: bool = True) -> None:
    conn.execute("UPDATE events SET status='DEFERRED', not_before=?, error=?, attempts=attempts-? WHERE id=?",
                 (now_iso(seconds), reason, 1 if refund_attempt else 0, event_id))
