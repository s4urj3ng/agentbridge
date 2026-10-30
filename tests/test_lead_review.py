"""A Tech Lead that also reviews always keeps one slot free for leading."""
from __future__ import annotations

from agentbridge.db import jdump
from agentbridge.orchestrator.quota import admission


def test_review_never_takes_the_last_tech_lead_slot(engine):
    c = engine.conn
    c.execute("UPDATE agents SET roles_json=?, max_concurrency=2 WHERE id='mock-lead'",
              (jdump(["tech_lead", "reviewer"]),))
    assert admission(c, "mock-lead", "review")[0]
    c.execute("INSERT INTO execution_attempts (agent_id, purpose, status, nonce, run_dir, cwd, started_at, deadline_at) "
              "VALUES ('mock-lead', 'review', 'RUNNING', 'n', '/tmp', '/tmp', '2026-10-30T00:00:00+00:00', '2099-01-01T00:00:00+00:00')")
    ok, why = admission(c, "mock-lead", "review")
    assert not ok and "tech lead slot" in why
    assert admission(c, "mock-lead", "tech_lead")[0]
