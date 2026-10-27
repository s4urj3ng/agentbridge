"""Read-only summaries of quota budgets and project progress (Telegram bot, dashboard, CLI).

Quota numbers are AgentBridge's own measurements against owner-configured
ceilings (rolling windows, daily/weekly limits). Subscription providers do not
expose real remaining quota, so none is invented. Provider-reported state
(cooldown after a limit error, auth failure, circuit breaker) is shown as-is.
"""
from __future__ import annotations

import fcntl
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from .db import jload, parse_ts, row, rows, utcnow
from .orchestrator.overrides import fallback_chain
from .orchestrator.quota import provider_block, running_count, window_count


def engine_running(data_dir: Path) -> bool:
    lock = data_dir / "engine.lock"
    if not lock.exists():
        return False
    with open(lock) as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def _windows(a: sqlite3.Row) -> list[dict[str, Any]]:
    cfg = jload(a["config_json"], {})
    wins = [{"label": _label(float(w["hours"])), "hours": float(w["hours"]), "max": int(w["max"])}
            for w in cfg.get("rolling_limits") or []]
    if a["daily_invocation_limit"] is not None:
        wins.append({"label": "24h", "hours": 24.0, "max": int(a["daily_invocation_limit"])})
    if a["weekly_invocation_limit"] is not None:
        wins.append({"label": "7d", "hours": 168.0, "max": int(a["weekly_invocation_limit"])})
    return wins


WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d"}


def _label(hours: float) -> str:
    return f"{hours / 24:g}d" if hours >= 24 and hours % 24 == 0 else f"{hours:g}h"


def quota_report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per enabled agent: budget windows with used / left %, plus provider-reported state."""
    out = []
    now = utcnow()
    agents = rows(conn, "SELECT * FROM agents WHERE enabled=1 ORDER BY id")
    if any(a["provider"] != "mock" for a in agents):   # hide demo identities once real ones exist
        agents = [a for a in agents if a["provider"] != "mock"]
    for a in agents:
        cfg = jload(a["config_json"], {})
        wins = []
        for w in _windows(a):
            used = window_count(conn, a["id"], w["hours"])
            left = max(0.0, 1 - used / w["max"]) if w["max"] > 0 else 0.0
            wins.append(w | {"used": used, "left_pct": round(left * 100)})
        state, until, a_reason = "ok", None, None
        cu, co = parse_ts(a["cooldown_until"]), parse_ts(a["circuit_open_until"])
        if a["paused"]:
            state = "paused"
        elif cu and cu > now:
            state, until = "cooldown", cu
        elif co and co > now:
            state, until = "circuit_open", co
        else:
            stop = provider_block(a, now)   # provider numbers past the owner's stop level
            if stop:
                state = "provider_stop"
                a_reason = stop
        last_limit = row(conn, "SELECT started_at, outcome FROM quota_usage WHERE agent_id=? AND outcome IN "
                               "('rate_limited','usage_limited') ORDER BY id DESC LIMIT 1", a["id"])
        week = row(conn, "SELECT COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o FROM quota_usage "
                         "WHERE agent_id=? AND started_at >= ?", a["id"],
                   (now - timedelta(days=7)).isoformat(timespec="seconds"))
        provider = []
        pq = jload(a["provider_quota_json"], None)
        for name, w in ((pq or {}).get("windows") or {}).items():
            resets = parse_ts(w.get("resets_at"))
            stale = bool(resets and resets <= now)   # window has reset since the last report
            used = w.get("used_pct")
            provider.append({"name": name, "label": WINDOW_LABELS.get(name, name), "used_pct": used,
                             "left_pct": None if used is None or stale else max(0, round(100 - used)),
                             "resets_at": w.get("resets_at"), "status": w.get("status"), "stale": stale})
        out.append({"id": a["id"], "provider": a["provider"], "model": cfg.get("model"), "state": state,
                    "fallback_to": fallback_chain(conn, a["id"], cfg),
                    "provider_windows": provider, "provider_source": (pq or {}).get("source"),
                    "provider_quota_at": a["provider_quota_at"],
                    "until": until.isoformat(timespec="minutes") if until else None,
                    "reason": a_reason if state == "provider_stop" else a["cooldown_reason"], "windows": wins, "running": running_count(conn, a["id"]),
                    "max_concurrency": a["max_concurrency"],
                    "last_limit_error": dict(last_limit) if last_limit else None,
                    "calls_7d": week["n"] or 0, "tokens_7d": (week["i"] or 0) + (week["o"] or 0)})
    return out


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    projects = []
    for p in rows(conn, "SELECT * FROM projects ORDER BY id"):
        mss = []
        for m in rows(conn, "SELECT * FROM milestones WHERE project_id=? AND status IN "
                            "('ACTIVE','AWAITING_APPROVAL','BLOCKED') ORDER BY id", p["id"]):
            counts = {r["state"]: r["c"] for r in rows(conn, "SELECT state, COUNT(*) c FROM tasks WHERE "
                                                             "milestone_id=? GROUP BY state", m["id"])}
            total = sum(counts.values())
            done = counts.get("COMPLETED", 0) + counts.get("CANCELLED", 0)
            mss.append({"key": m["key"], "status": m["status"], "goal": m["goal"], "total": total, "done": done,
                        "pct": round(100 * done / total) if total else 0, "counts": counts,
                        "running": [dict(r) for r in rows(conn, "SELECT key, assignee, state FROM tasks WHERE "
                                                                "milestone_id=? AND state IN ('DISPATCHED','RUNNING',"
                                                                "'VALIDATING') ORDER BY id", m["id"])],
                        "review": [dict(r) for r in rows(conn, "SELECT key, reviewer FROM tasks WHERE milestone_id=? "
                                                               "AND state='REVIEW_PENDING' ORDER BY id", m["id"])],
                        "blocked": [dict(r) for r in rows(conn, "SELECT key, blocked_reason FROM tasks WHERE "
                                                                "milestone_id=? AND state='BLOCKED' ORDER BY id",
                                                          m["id"])]})
        last_done = row(conn, "SELECT key, completed_at FROM milestones WHERE project_id=? AND status='COMPLETED' "
                              "ORDER BY completed_at DESC LIMIT 1", p["id"])
        projects.append({"slug": p["slug"], "name": p["name"], "paused": bool(p["paused"]), "milestones": mss,
                         "last_completed": dict(last_done) if last_done else None})
    return {"projects": projects,
            "pending_approvals": row(conn, "SELECT COUNT(*) c FROM approval_requests WHERE status='PENDING'")["c"],
            "queued": row(conn, "SELECT COUNT(*) c FROM tasks WHERE state IN ('CREATED','QUEUED')")["c"]}
