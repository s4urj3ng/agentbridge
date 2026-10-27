"""Free quota probes: provider commands that report real quota without a model turn.

Currently: Antigravity `/usage` (0 tokens). One call serves every Antigravity
identity (they share the host login); each identity gets the group governing
its model. Safe to run often; used by the engine periodically and by the
Telegram bot when the owner opens the quota view.
"""
from __future__ import annotations

import sqlite3

from .adapters.registry import make_adapter
from .config import Settings
from .db import connect, jload, now_iso, parse_ts, row, rows, tx, utcnow
from .orchestrator.quota import record_provider_quota

PROBE_KEY = "quota.probe_at"


def refresh_free_quotas(settings: Settings, conn: sqlite3.Connection | None = None) -> dict[str, dict | None]:
    own = conn is None
    conn = conn or connect(settings.db_path)
    try:
        results: dict[str, dict | None] = {}
        cache: dict[str, dict | None] = {}
        for a in rows(conn, "SELECT id, config_json FROM agents WHERE enabled=1 AND provider='antigravity'"):
            ad = make_adapter("antigravity", a["id"], jload(a["config_json"], {}))
            key = ad.executable or ""
            if key not in cache:
                cache[key] = ad.read_usage_cached(max_age_s=30)
            results[a["id"]] = ad.quota_for(cache[key])
        with tx(conn):
            for agent_id, pq in results.items():
                record_provider_quota(conn, agent_id, pq)
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (PROBE_KEY, now_iso()))
        return results
    finally:
        if own:
            conn.close()


def probe_age_s(conn: sqlite3.Connection) -> float | None:
    r = row(conn, "SELECT value FROM settings WHERE key=?", PROBE_KEY)
    ts = parse_ts(r["value"]) if r else None
    return (utcnow() - ts).total_seconds() if ts else None
