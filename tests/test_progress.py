"""Task replacement (counted once) and the Telegram progress view."""
from __future__ import annotations

import pytest

from agentbridge.db import row, tx
from agentbridge.integrations.telegram import MAIN_MENU
from agentbridge.orchestrator.decisions import parse_envelope
from agentbridge.reporting import milestone_progress

from .conftest import q, run
from .test_telegram import OWNER, _cb, setup  # noqa: F401  (fixture)


def _ids(e, mid):
    return {t["key"]: t["id"] for t in q(e, "SELECT key, id FROM tasks WHERE milestone_id=?", mid)}


def _start(e):
    e.conn.execute("UPDATE agents SET paused=1 WHERE id IN ('mock-worker','mock-worker-b')")   # tasks stay open
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "PG1")
    run(e, 20)
    return mid


def test_tech_lead_replaces_blocked_task(setup):  # noqa: F811
    e, bot, fake = setup
    mid = _start(e)
    ids = _ids(e, mid)
    with tx(e.conn):
        e.conn.execute("UPDATE tasks SET state='BLOCKED', blocked_reason='revision limit' WHERE id=?", (ids["fix-add"],))
    worker = q(e, "SELECT assignee FROM tasks WHERE id=?", ids["fix-add"])[0]["assignee"]
    env = parse_envelope({"rationale": "r", "decisions": [{"type": "CREATE_TASKS", "tasks": [{
        "key": "fix-add-r2", "title": "Fix add() again", "description": "Same scope, fresh task.",
        "assignee": worker, "acceptance_criteria": ["add(2, 3) == 5"], "replaces": "fix-add"}]}]})
    applied, rejected = e.apply_decisions(env, None, row(e.conn, "SELECT * FROM projects"),
                                          row(e.conn, "SELECT * FROM milestones WHERE id=?", mid), None)
    assert applied == 1 and not rejected
    ids = _ids(e, mid)
    assert q(e, "SELECT state FROM tasks WHERE id=?", ids["fix-add"]) == [{"state": "CANCELLED"}]
    assert q(e, "SELECT depends_on_id FROM task_dependencies WHERE task_id=?", ids["impl-mul"]) == \
        [{"depends_on_id": ids["fix-add-r2"]}]
    pr = milestone_progress(e.conn, mid)
    assert pr["total"] == 2 and pr["replaced"] == 1          # the replaced task is not counted

    # a completed task cannot be replaced; unknown keys are refused
    with tx(e.conn):
        e.conn.execute("UPDATE tasks SET state='COMPLETED' WHERE id=?", (ids["fix-add-r2"],))
    bad = parse_envelope({"rationale": "r", "decisions": [{"type": "CREATE_TASKS", "tasks": [{
        "key": "x-r3", "title": "Again", "description": "again", "assignee": worker,
        "acceptance_criteria": ["ok"], "replaces": key}]} for key in ("fix-add-r2", "nope")]})
    applied, rejected = e.apply_decisions(bad, None, row(e.conn, "SELECT * FROM projects"),
                                          row(e.conn, "SELECT * FROM milestones WHERE id=?", mid), None)
    assert applied == 0 and len(rejected) == 2
    pr = milestone_progress(e.conn, mid)
    assert (pr["done"], pr["total"], pr["pct"]) == (1, 2, 50)


def test_owner_replace_cli_and_validation(setup):  # noqa: F811
    e, bot, fake = setup
    mid = _start(e)
    ids = _ids(e, mid)
    with pytest.raises(ValueError):
        e.replace_task(ids["fix-add"], ids["fix-add"], "owner:test")
    e.replace_task(ids["fix-add"], ids["impl-mul"], "owner:test")
    assert q(e, "SELECT state FROM tasks WHERE id=?", ids["fix-add"]) == [{"state": "CANCELLED"}]
    assert q(e, "SELECT * FROM task_dependencies WHERE task_id=?", ids["impl-mul"]) == []   # no self-dependency
    assert q(e, "SELECT actor FROM audit_log WHERE action='task.replace'") == [{"actor": "owner:test"}]


def test_progress_view(setup):  # noqa: F811
    e, bot, fake = setup
    assert any(b["callback_data"] == "progress" and "Tiến độ" in b["text"] for r in MAIN_MENU for b in r)
    mid = _start(e)
    ids = _ids(e, mid)
    with tx(e.conn):
        e.conn.execute("UPDATE tasks SET state='COMPLETED' WHERE id=?", (ids["fix-add"],))
    fake.updates = [_cb(OWNER, "progress")]
    bot.poll_once(timeout=0)
    text = fake.calls[-1][1]["text"]
    assert "📈 Tiến độ" in text and "PG1" in text and "🟢 đang chạy" in text
    assert "<b>50%</b> · 1/2 task xong" in text and "✅ fix-add" in text and "impl-mul" in text
