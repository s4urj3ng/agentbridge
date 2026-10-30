"""policy.escalate_after_revisions: a task that keeps coming back moves to a stronger worker."""
from __future__ import annotations

from .conftest import make_engine, q, register, run

GOAL = "Fix add() and implement mul() in calc.py with tests"
ESC = {"escalate_after_revisions": 1, "escalation_worker": "mock-strong"}


def _engine(settings, tmp_path, **policy):
    settings.agents.append({"id": "mock-strong", "provider": "mock", "roles": ["worker"], "max_concurrency": 1})
    e = make_engine(settings)
    register(e, tmp_path, extra_policy=policy)
    return e


def test_revised_task_moves_to_escalation_worker(settings, tmp_path):
    e = _engine(settings, tmp_path, **ESC)
    mid = e.submit_goal("demo-calc", GOAL, "E1")
    run(e, 120)
    tasks = {t["key"]: t for t in q(e, "SELECT * FROM tasks WHERE milestone_id=?", mid)}
    assert tasks["fix-add"]["revision_count"] == 1 and tasks["fix-add"]["assignee"] == "mock-strong"
    assert tasks["fix-add"]["state"] == "COMPLETED"
    assert tasks["impl-mul"]["assignee"] != "mock-strong"              # never revised: stays put
    esc = q(e, "SELECT detail_json FROM audit_log WHERE action='task.escalate'")
    assert len(esc) == 1 and '"to": "mock-strong"' in esc[0]["detail_json"]


def test_no_escalation_when_worker_unavailable_or_off(settings, tmp_path):
    e = _engine(settings, tmp_path, **ESC)
    e.conn.execute("UPDATE agents SET cooldown_until='2099-01-01T00:00:00+00:00', cooldown_reason='usage_limited' "
                   "WHERE id='mock-strong'")
    mid = e.submit_goal("demo-calc", GOAL, "E2")
    run(e, 120)
    assert q(e, "SELECT assignee FROM tasks WHERE milestone_id=? AND key='fix-add'", mid)[0]["assignee"] != "mock-strong"
    assert q(e, "SELECT 1 FROM audit_log WHERE action='task.escalate_skipped'")


def test_escalation_off_by_default(settings, tmp_path):
    e = _engine(settings, tmp_path)
    mid = e.submit_goal("demo-calc", GOAL, "E3")
    run(e, 120)
    assert "mock-strong" not in {t["assignee"] for t in q(e, "SELECT assignee FROM tasks WHERE milestone_id=?", mid)}
