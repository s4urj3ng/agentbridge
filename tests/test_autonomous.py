"""Autonomous projects (no owner questions), milestone cancel, goal text from stdin."""
from __future__ import annotations

import io

from agentbridge import cli
from agentbridge.db import row, tx
from agentbridge.orchestrator.context import build_tech_lead_prompt
from agentbridge.orchestrator.decisions import parse_envelope

from .conftest import make_engine, q, register, run


def _event(e, mid):
    with tx(e.conn):
        eid = e.conn.execute("INSERT INTO events (type, project_id, milestone_id, payload_json, status, created_at) "
                             "SELECT 'TASK_BLOCKED', project_id, id, '{}', 'DONE', '2026-01-01' FROM milestones "
                             "WHERE id=?", (mid,)).lastrowid
    return row(e.conn, "SELECT * FROM events WHERE id=?", eid)


def test_autonomous_rejects_owner_questions_but_allows_scope_change(settings, tmp_path):
    e = make_engine(settings)
    register(e, tmp_path, extra_policy={"autonomous": True})
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "AU1")
    run(e, 120)
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"   # starts, no approval
    mid2 = e.submit_goal("demo-calc", "Something else", "AU2")
    project = row(e.conn, "SELECT * FROM projects WHERE slug='demo-calc'")
    ms = row(e.conn, "SELECT * FROM milestones WHERE id=?", mid2)
    ev = _event(e, mid2)
    env = parse_envelope({"rationale": "r", "decisions": [
        {"type": "REQUEST_HUMAN_APPROVAL", "action": "unblock_task", "reason": "may I unblock?"},
        {"type": "REQUEST_HUMAN_APPROVAL", "action": "other", "reason": "pick one of A/B/C"},
        {"type": "REQUEST_HUMAN_APPROVAL", "action": "scope_change", "reason": "goal is impossible as written"}]})
    applied, rejected = e.apply_decisions(env, ev, project, ms, None)
    assert applied == 1 and len(rejected) == 2 and all("autonomous mode" in r for r in rejected)
    assert [a["action"] for a in q(e, "SELECT action FROM approval_requests WHERE milestone_id=?", mid2)] == ["scope_change"]
    prompt = build_tech_lead_prompt(e.conn, project=project, milestone=ms, event=ev,
                                    manifest={"autonomous": True}, agents={}, limits={})
    assert "Autonomous mode (owner-authorised)" in prompt
    assert "Autonomous mode" not in build_tech_lead_prompt(e.conn, project=project, milestone=ms, event=ev,
                                                           manifest={}, agents={}, limits={})


def test_not_autonomous_keeps_owner_questions(settings, tmp_path):
    e = make_engine(settings)
    register(e, tmp_path)
    mid = e.submit_goal("demo-calc", "Something", "AU3")
    env = parse_envelope({"rationale": "r", "decisions": [
        {"type": "REQUEST_HUMAN_APPROVAL", "action": "unblock_task", "reason": "may I unblock?"}]})
    applied, _ = e.apply_decisions(env, _event(e, mid), row(e.conn, "SELECT * FROM projects"),
                                   row(e.conn, "SELECT * FROM milestones WHERE id=?", mid), None)
    assert applied == 1


def test_cancel_milestone_cli(settings, tmp_path, capsys, monkeypatch):
    e = make_engine(settings)
    register(e, tmp_path)
    e.conn.execute("UPDATE agents SET paused=1 WHERE id='mock-worker'")      # tasks stay open
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "AU4")
    run(e, 20)
    with tx(e.conn):
        e._request_approval(q(e, "SELECT id FROM projects")[0]["id"], "other", "pick one", "tech_lead", milestone_id=mid)
    base = ["--data-dir", str(settings.data_dir)]
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    cli.main(base + ["milestone", "cancel", "AU4"])
    assert "tasks cancelled: fix-add, impl-mul" in capsys.readouterr().out
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "CANCELLED"
    assert {t["state"] for t in q(e, "SELECT state FROM tasks WHERE milestone_id=?", mid)} == {"CANCELLED"}
    assert q(e, "SELECT status FROM approval_requests WHERE milestone_id=?", mid) == [{"status": "EXPIRED"}]
    assert q(e, "SELECT actor FROM audit_log WHERE action='milestone.cancel'") == [{"actor": "owner:cli"}]
    # goal text from stdin ("-"), e.g. a hand-over written elsewhere
    monkeypatch.setattr("sys.stdin", io.StringIO("Continue the port from the hand-over notes.\nLine two."))
    cli.main(base + ["goal", "demo-calc", "-", "--milestone", "AU5"])
    assert q(e, "SELECT goal FROM milestones WHERE key='AU5'")[0]["goal"].endswith("Line two.")
