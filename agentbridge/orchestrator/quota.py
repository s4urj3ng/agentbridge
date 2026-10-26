"""Quota-aware admission control.

Subscription quotas are not machine-readable, so nothing here claims to know
"remaining quota". We count what we can measure (invocations, durations,
reported tokens, limit errors, retry-after) and enforce owner-configured
ceilings, cooldowns and circuit breakers. Per-agent: a cooldown on one agent
never blocks another.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from ..db import audit, now_iso, parse_ts, row, tx, utcnow

QUOTA_OUTCOMES = {"rate_limited", "usage_limited"}
DEFAULT_COOLDOWN = {"rate_limited": 120, "usage_limited": 1800}
MAX_COOLDOWN_S = 12 * 3600
CIRCUIT_THRESHOLD = 3
CIRCUIT_OPEN_S = 600


def invocation_counts(conn: sqlite3.Connection, agent_id: str) -> tuple[int, int, int]:
    """(last 24h, last 7d, last 24h review) invocation counts."""
    day = (utcnow() - timedelta(days=1)).isoformat(timespec="seconds")
    week = (utcnow() - timedelta(days=7)).isoformat(timespec="seconds")
    r = row(conn, """SELECT
            SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS d,
            COUNT(*) AS w,
            SUM(CASE WHEN started_at >= ? AND purpose='review' THEN 1 ELSE 0 END) AS dr
        FROM quota_usage WHERE agent_id=? AND started_at >= ?""", day, day, agent_id, week)
    return int(r["d"] or 0), int(r["w"] or 0), int(r["dr"] or 0)


def running_count(conn: sqlite3.Connection, agent_id: str) -> int:
    return row(conn, "SELECT COUNT(*) c FROM execution_attempts WHERE agent_id=? AND status IN ('STARTING','RUNNING')",
               agent_id)["c"]


def admission(conn: sqlite3.Connection, agent_id: str, purpose: str) -> tuple[bool, str]:
    a = row(conn, "SELECT * FROM agents WHERE id=?", agent_id)
    if a is None:
        return False, "unknown agent"
    if not a["enabled"]:
        return False, "disabled"
    if a["paused"]:
        return False, "paused"
    now = utcnow()
    cu = parse_ts(a["cooldown_until"])
    if cu and cu > now:
        return False, f"cooldown until {a['cooldown_until']} ({a['cooldown_reason']})"
    co = parse_ts(a["circuit_open_until"])
    if co and co > now:
        return False, f"circuit open until {a['circuit_open_until']}"
    if running_count(conn, agent_id) >= a["max_concurrency"]:
        return False, "at max concurrency"
    d, w, _dr = invocation_counts(conn, agent_id)
    if a["daily_invocation_limit"] is not None:
        limit = a["daily_invocation_limit"]
        # reserve the last N daily invocations for reviews
        if purpose != "review":
            limit -= a["reserved_review_invocations"]
        if d >= limit:
            return False, f"daily invocation budget reached ({d}/{a['daily_invocation_limit']}, purpose={purpose})"
    if a["weekly_invocation_limit"] is not None and w >= a["weekly_invocation_limit"]:
        return False, f"weekly invocation budget reached ({w}/{a['weekly_invocation_limit']})"
    return True, "ok"


def record_start(conn: sqlite3.Connection, agent_id: str, attempt_id: int, purpose: str) -> None:
    conn.execute("INSERT INTO quota_usage (agent_id, attempt_id, purpose, started_at) VALUES (?,?,?,?)",
                 (agent_id, attempt_id, purpose, now_iso()))


def record_end(conn: sqlite3.Connection, attempt_id: int, outcome: str, duration_s: float | None,
               in_tok: int | None, out_tok: int | None, retry_after: int | None) -> None:
    conn.execute("UPDATE quota_usage SET outcome=?, duration_s=?, input_tokens=?, output_tokens=?, retry_after_s=? "
                 "WHERE attempt_id=?", (outcome, duration_s, in_tok, out_tok, retry_after, attempt_id))


def on_outcome(conn: sqlite3.Connection, agent_id: str, outcome: str, retry_after: int | None) -> None:
    with tx(conn):
        a = row(conn, "SELECT * FROM agents WHERE id=?", agent_id)
        if a is None:
            return
        if outcome == "success":
            conn.execute("UPDATE agents SET consecutive_failures=0, quota_error_streak=0, updated_at=? WHERE id=?",
                         (now_iso(), agent_id))
            return
        if outcome in QUOTA_OUTCOMES:
            streak = a["quota_error_streak"] + 1
            base = retry_after if retry_after else DEFAULT_COOLDOWN[outcome] * (2 ** (streak - 1))
            secs = min(max(base, 30), MAX_COOLDOWN_S)
            conn.execute("UPDATE agents SET cooldown_until=?, cooldown_reason=?, quota_error_streak=?, updated_at=? "
                         "WHERE id=?", (now_iso(secs), outcome, streak, now_iso(), agent_id))
            audit(conn, "quota", "agent.cooldown", agent=agent_id, outcome=outcome, seconds=secs)
            return
        if outcome == "auth":
            conn.execute("UPDATE agents SET paused=1, cooldown_reason='auth failure: re-login required', updated_at=? "
                         "WHERE id=?", (now_iso(), agent_id))
            audit(conn, "quota", "agent.paused_auth", agent=agent_id)
            return
        fails = a["consecutive_failures"] + 1
        open_until = now_iso(CIRCUIT_OPEN_S) if fails >= CIRCUIT_THRESHOLD else a["circuit_open_until"]
        conn.execute("UPDATE agents SET consecutive_failures=?, circuit_open_until=?, updated_at=? WHERE id=?",
                     (fails, open_until, now_iso(), agent_id))
        if fails >= CIRCUIT_THRESHOLD:
            audit(conn, "quota", "agent.circuit_open", agent=agent_id, failures=fails)


def set_paused(conn: sqlite3.Connection, agent_id: str, paused: bool, actor: str) -> None:
    with tx(conn):
        conn.execute("UPDATE agents SET paused=?, updated_at=? WHERE id=?", (int(paused), now_iso(), agent_id))
        if not paused:
            conn.execute("UPDATE agents SET cooldown_until=NULL, circuit_open_until=NULL, consecutive_failures=0, "
                         "quota_error_streak=0 WHERE id=?", (agent_id,))
        audit(conn, actor, "agent.pause" if paused else "agent.resume", agent=agent_id)
