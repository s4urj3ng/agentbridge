"""An exhausted weekly provider window also empties the 5h window (display and admission)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentbridge.orchestrator.quota import effective_windows

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
LATER = (NOW + timedelta(days=2)).isoformat()


def test_weekly_zero_caps_five_hour():
    pq = {"windows": {"five_hour": {"used_pct": 0.0, "resets_at": (NOW + timedelta(hours=3)).isoformat()},
                      "seven_day": {"used_pct": 100.0, "resets_at": LATER}}}
    w = effective_windows(pq, NOW)
    assert w["five_hour"]["used_pct"] == 100.0 and w["five_hour"]["status"] == "rejected"
    assert w["five_hour"]["resets_at"] == LATER
    assert pq["windows"]["five_hour"]["used_pct"] == 0.0           # the stored report is untouched
    shown_zero = {"windows": {"five_hour": {"used_pct": 11.0}, "seven_day": {"used_pct": 99.6, "resets_at": LATER}}}
    assert effective_windows(shown_zero, NOW)["five_hour"]["used_pct"] == 100.0


def test_weekly_left_or_stale_keeps_five_hour():
    pq = {"windows": {"five_hour": {"used_pct": 10.0}, "seven_day": {"used_pct": 99.0, "resets_at": LATER}}}
    assert effective_windows(pq, NOW)["five_hour"]["used_pct"] == 10.0
    stale = {"windows": {"five_hour": {"used_pct": 10.0},
                         "seven_day": {"status": "rejected", "resets_at": (NOW - timedelta(hours=1)).isoformat()}}}
    assert effective_windows(stale, NOW)["five_hour"]["used_pct"] == 10.0
    assert effective_windows(None, NOW) == {}


def test_admission_refuses_on_capped_window(engine):
    from agentbridge.db import jdump, row
    from agentbridge.orchestrator.quota import provider_block
    engine.conn.execute("UPDATE agents SET provider_quota_json=? WHERE id='mock-worker'", (jdump({"windows": {
        "five_hour": {"used_pct": 0.0}, "seven_day": {"used_pct": 100.0, "resets_at": LATER}}}),))
    why = provider_block(row(engine.conn, "SELECT * FROM agents WHERE id='mock-worker'"), NOW)
    assert why and "exhausted" in why and LATER in why
