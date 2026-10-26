"""Acceptance 10: quota exhaustion pauses the right agent only."""
from __future__ import annotations

from agentbridge.orchestrator import quota

from .conftest import make_engine, q, register, run, script_agent

TWO_INDEPENDENT = {"rationale": "two independent tasks", "decisions": [{"type": "CREATE_TASKS", "tasks": [
    {"key": "notes-a", "title": "Write notes A", "kind": "research", "assignee": "mock-worker",
     "description": "Write notes A", "acceptance_criteria": ["file exists"], "validation": []},
    {"key": "notes-b", "title": "Write notes B", "kind": "research", "assignee": "mock-worker-b",
     "description": "Write notes B", "acceptance_criteria": ["file exists"], "validation": []}]}]}


def test_usage_limit_pauses_only_that_agent(settings, tmp_path):
    for a in settings.agents:
        if a["id"] == "mock-worker":
            a["fail_mode"] = "usage_limit"
            a["fail_times"] = 99
    script_agent(settings, "mock-lead", [TWO_INDEPENDENT] + [
        {"rationale": "ok", "decisions": [{"type": "APPROVE_TASK", "task_id": 2}]}], tmp_path)
    e = make_engine(settings)
    register(e, tmp_path)
    e.submit_goal("demo-calc", "two notes", "Q1")
    run(e, 30)
    a = q(e, "SELECT * FROM agents WHERE id='mock-worker'")[0]
    assert a["cooldown_reason"] == "usage_limited" and a["cooldown_until"]
    assert q(e, "SELECT retry_after_s FROM quota_usage WHERE agent_id='mock-worker'")[0]["retry_after_s"] == 600
    ok, why = quota.admission(e.conn, "mock-worker", "work")
    assert not ok and "cooldown" in why
    # the unrelated agent kept working and its task completed
    tb = q(e, "SELECT state FROM tasks WHERE key='notes-b'")[0]
    assert tb["state"] == "COMPLETED"
    ta = q(e, "SELECT state, quota_requeues FROM tasks WHERE key='notes-a'")[0]
    assert ta["state"] == "QUEUED" and ta["quota_requeues"] == 1   # waits for cooldown; not retried in a loop
    assert q(e, "SELECT COUNT(*) c FROM execution_attempts WHERE agent_id='mock-worker'")[0]["c"] == 1
    assert quota.admission(e.conn, "mock-worker-b", "work")[0]
    # manual resume clears the cooldown
    quota.set_paused(e.conn, "mock-worker", False, "owner:test")
    assert quota.admission(e.conn, "mock-worker", "work")[0]


def test_daily_budget_and_reserved_review_capacity(engine):
    from agentbridge.db import now_iso
    engine.conn.execute("UPDATE agents SET daily_invocation_limit=5, reserved_review_invocations=2 WHERE id='mock-reviewer'")
    for i in range(3):
        engine.conn.execute("INSERT INTO quota_usage (agent_id, purpose, started_at) VALUES ('mock-reviewer','work',?)",
                            (now_iso(),))
    assert not quota.admission(engine.conn, "mock-reviewer", "work")[0]   # 3 >= 5-2 reserved
    assert quota.admission(engine.conn, "mock-reviewer", "review")[0]     # reviews may use the reserve
    for i in range(2):
        engine.conn.execute("INSERT INTO quota_usage (agent_id, purpose, started_at) VALUES ('mock-reviewer','review',?)",
                            (now_iso(),))
    assert not quota.admission(engine.conn, "mock-reviewer", "review")[0]


def test_circuit_breaker_and_bounded_retries(settings, tmp_path):
    for a in settings.agents:
        if a["id"] == "mock-worker":
            a["fail_mode"] = "crash"
            a["fail_times"] = 99
    e = make_engine(settings)
    register(e, tmp_path)
    e.submit_goal("demo-calc", "Fix add()", "Q2")
    run(e, 30)
    n = q(e, "SELECT COUNT(*) c FROM execution_attempts WHERE agent_id='mock-worker'")[0]["c"]
    assert n == 3  # max_execution_failures, then stop
    a = q(e, "SELECT consecutive_failures, circuit_open_until FROM agents WHERE id='mock-worker'")[0]
    assert a["consecutive_failures"] == 3 and a["circuit_open_until"]
    t = q(e, "SELECT state FROM tasks WHERE key='fix-add'")[0]
    assert t["state"] == "BLOCKED"
    # TL was informed and escalated to a human
    assert q(e, "SELECT COUNT(*) c FROM approval_requests WHERE action='unblock_task'")[0]["c"] == 1


def test_auth_failure_pauses_agent(settings, tmp_path):
    for a in settings.agents:
        if a["id"] == "mock-worker":
            a["fail_mode"] = "auth"
            a["fail_times"] = 1
    e = make_engine(settings)
    register(e, tmp_path)
    e.submit_goal("demo-calc", "Fix add()", "Q3")
    run(e, 30)
    a = q(e, "SELECT paused, cooldown_reason FROM agents WHERE id='mock-worker'")[0]
    assert a["paused"] == 1 and "re-login" in a["cooldown_reason"]
    assert q(e, "SELECT state FROM tasks WHERE key='fix-add'")[0]["state"] == "BLOCKED"


def test_tech_lead_rate_limit_defers_event(settings, tmp_path):
    for a in settings.agents:
        if a["id"] == "mock-lead":
            a["fail_mode"] = "rate_limit"
            a["fail_times"] = 1
    e = make_engine(settings)
    register(e, tmp_path)
    e.submit_goal("demo-calc", "Fix add()", "Q4")
    run(e, 10)
    ev = q(e, "SELECT status, not_before FROM events WHERE type='GOAL_SUBMITTED'")[0]
    assert ev["status"] == "DEFERRED" and ev["not_before"]
    assert q(e, "SELECT cooldown_reason FROM agents WHERE id='mock-lead'")[0]["cooldown_reason"] == "rate_limited"
