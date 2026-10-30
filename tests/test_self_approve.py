"""policy.self_approve: the owner delegates approvals to the Tech Lead."""
from __future__ import annotations

from agentbridge.db import row, tx
from agentbridge.orchestrator.context import build_tech_lead_prompt
from agentbridge.orchestrator.decisions import parse_envelope

from .conftest import make_engine, q, register, run
from .test_autonomous import _event

SELF = {"autonomous": True, "self_approve": True, "draft_pr": "approval"}


def test_draft_pr_and_scope_change_approved_by_tech_lead(settings, tmp_path):
    e = make_engine(settings)
    register(e, tmp_path, extra_policy=SELF)
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", "SA1")
    run(e, 120)
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
    assert q(e, "SELECT action, status, resolved_by FROM approval_requests WHERE milestone_id=?", mid) == [
        {"action": "create_draft_pr", "status": "APPROVED", "resolved_by": "tech_lead:self"}]
    assert q(e, "SELECT 1 FROM events WHERE type='APPROVAL_RESOLVED' AND milestone_id=?", mid)

    e.conn.execute("UPDATE agents SET paused=1 WHERE id='mock-worker'")      # keep the next milestone open
    mid2 = e.submit_goal("demo-calc", "Something else", "SA2")
    env = parse_envelope({"rationale": "r", "decisions": [
        {"type": "REQUEST_HUMAN_APPROVAL", "action": "scope_change", "reason": "narrow the goal to add()"}]})
    e.apply_decisions(env, _event(e, mid2), row(e.conn, "SELECT * FROM projects"),
                      row(e.conn, "SELECT * FROM milestones WHERE id=?", mid2), None)
    assert q(e, "SELECT status, resolved_by FROM approval_requests WHERE milestone_id=?", mid2) == [
        {"status": "APPROVED", "resolved_by": "tech_lead:self"}]
    run(e, 20)
    assert q(e, "SELECT 1 FROM events WHERE type='HUMAN_DECISION' AND milestone_id=?", mid2)
    # the mock lead keeps answering the decision with an owner question (rejected in autonomous mode):
    # the engine's "could not decide" stops are lifted by the Tech Lead a bounded number of times
    lifted = q(e, "SELECT COUNT(*) c FROM approval_requests WHERE milestone_id=? AND action='other' "
                  "AND resolved_by='tech_lead:self'", mid2)[0]["c"]
    assert lifted == e.SELF_RESUME_MAX
    assert q(e, "SELECT action FROM approval_requests WHERE status='PENDING'") == [{"action": "other"}]


def test_engine_stops_self_resume_is_bounded(settings, tmp_path):
    e = make_engine(settings)
    register(e, tmp_path, extra_policy=SELF)
    e.conn.execute("UPDATE agents SET paused=1 WHERE id='mock-worker'")
    mid = e.submit_goal("demo-calc", "Fix add()", "SA3")
    pid = q(e, "SELECT id FROM projects")[0]["id"]
    with tx(e.conn):
        for i in range(e.SELF_RESUME_MAX + 1):
            e._request_approval(pid, "other", f"stop {i}", "engine", milestone_id=mid, dedupe_key=f"t{i}")
    st = [a["status"] for a in q(e, "SELECT status FROM approval_requests WHERE milestone_id=? ORDER BY id", mid)]
    assert st == ["APPROVED"] * e.SELF_RESUME_MAX + ["PENDING"]


def test_off_by_default_and_prompt(settings, tmp_path):
    e = make_engine(settings)
    register(e, tmp_path, extra_policy={"autonomous": True, "draft_pr": "approval"})
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", "SA4")
    run(e, 120)
    assert q(e, "SELECT status FROM approval_requests WHERE milestone_id=?", mid) == [{"status": "PENDING"}]
    project, ms = row(e.conn, "SELECT * FROM projects"), row(e.conn, "SELECT * FROM milestones WHERE id=?", mid)
    ev = _event(e, mid)
    on = build_tech_lead_prompt(e.conn, project=project, milestone=ms, event=ev,
                                manifest={"self_approve": True}, agents={}, limits={})
    off = build_tech_lead_prompt(e.conn, project=project, milestone=ms, event=ev, manifest={}, agents={}, limits={})
    assert "Approvals delegated to you" in on and "Approvals delegated to you" not in off
