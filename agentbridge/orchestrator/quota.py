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

from ..db import audit, jdump, jload, now_iso, parse_ts, row, tx, utcnow

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
    running = running_count(conn, agent_id)
    if running >= a["max_concurrency"]:
        return False, "at max concurrency"
    if purpose == "review" and "tech_lead" in jload(a["roles_json"], []) and running >= a["max_concurrency"] - 1:
        return False, "tech lead slot kept free (reviews use the other slots)"
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
    # Quota the provider itself reported on the last call (Claude rate_limit_event, ...).
    blocked = provider_block(a, now)
    if blocked:
        return False, blocked
    # Provider-shaped rolling windows, e.g. [{hours: 5, max: 25}, {hours: 168, max: 150}].
    # These are owner-chosen ceilings to spread usage, not a claim about the provider's real quota.
    for lim in jload(a["config_json"], {}).get("rolling_limits") or []:
        hours, cap = float(lim["hours"]), int(lim["max"])
        if purpose != "review":
            cap -= int(lim.get("reserve_for_review", 0))
        n = window_count(conn, agent_id, hours)
        if n >= cap:
            return False, f"{hours:g}h window budget reached ({n}/{lim['max']}, purpose={purpose})"
    return True, "ok"


def is_quota_block(why: str) -> bool:
    """Refusals caused by quota/health (worth falling back), as opposed to paused/disabled/busy."""
    return why.startswith(("cooldown", "circuit", "daily invocation", "weekly invocation", "provider window")) \
        or "window budget" in why


# Left below which a window counts as empty: the weekly pool at 1-2 % does not finish a task (owner,
# 2026-10-30); other windows count as empty once they show 0 %.
EMPTY_LEFT_PCT = {"seven_day": 2.0}
EMPTY_LEFT_DEFAULT = 0.5


def _exhausted(w: dict, name: str = "") -> bool:
    if w.get("status") == "rejected":
        return True
    used = w.get("used_pct")
    return used is not None and 100 - used <= EMPTY_LEFT_PCT.get(name, EMPTY_LEFT_DEFAULT)


def effective_windows(pq: dict | None, now) -> dict[str, dict]:
    """Provider windows as they constrain work: an exhausted weekly window also empties the 5h one.

    Providers keep reporting the 5h window as fresh while the weekly pool is empty, yet no call
    succeeds until the weekly reset, so 5h is shown and treated as 0 % left until then.
    """
    wins = {k: dict(v) for k, v in ((pq or {}).get("windows") or {}).items()}
    week = wins.get("seven_day")
    if week and _exhausted(week, "seven_day"):
        resets = parse_ts(week.get("resets_at"))
        if not (resets and resets <= now):
            week["status"] = "rejected"
            five = wins.setdefault("five_hour", {})
            five.update(used_pct=100.0, status="rejected", resets_at=week.get("resets_at"), capped_by="seven_day")
    return wins


def provider_block(a: sqlite3.Row, now) -> str | None:
    """Refuse when the provider's own report says a window is exhausted or above the owner's stop level.

    `provider_stop_pct` (agent config) is a number or a per-window map, e.g.
    {five_hour: 90, seven_day: 85}: stop sending work before the provider refuses, so a fallback
    agent takes over instead of draining the weekly pool. Windows whose reset time has passed
    are ignored (the report is stale).
    """
    pq = jload(a["provider_quota_json"], None)
    if not pq:
        return None
    stop = jload(a["config_json"], {}).get("provider_stop_pct")
    for name, w in effective_windows(pq, now).items():
        resets = parse_ts(w.get("resets_at"))
        if resets and resets <= now:
            continue
        until = f" until {w['resets_at']}" if w.get("resets_at") else ""
        if _exhausted(w, name):
            return f"provider window {name} exhausted{until}"
        limit = stop.get(name) if isinstance(stop, dict) else stop
        if limit is not None and w.get("used_pct") is not None and w["used_pct"] >= float(limit):
            return f"provider window {name} at {w['used_pct']:g}% (stop at {limit}%){until}"
    return None


def record_provider_quota(conn: sqlite3.Connection, agent_id: str, pq: dict | None) -> None:
    if pq:
        conn.execute("UPDATE agents SET provider_quota_json=?, provider_quota_at=? WHERE id=?",
                     (jdump(pq), now_iso(), agent_id))


def window_count(conn: sqlite3.Connection, agent_id: str, hours: float) -> int:
    since = (utcnow() - timedelta(hours=hours)).isoformat(timespec="seconds")
    return row(conn, "SELECT COUNT(*) c FROM quota_usage WHERE agent_id=? AND started_at >= ?", agent_id, since)["c"]


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
