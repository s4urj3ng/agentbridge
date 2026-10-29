"""policy.auto_next: the Tech Lead plans and starts the next milestone itself, stacked on unmerged work."""
from __future__ import annotations

from agentbridge.workers.git_ops import git

from .conftest import make_engine, q, register, run, script_agent

AUTO = {"autonomous": True, "auto_next": True}


def test_auto_next_chains_stacks_and_stops(settings, tmp_path):
    e = make_engine(settings)
    repo = register(e, tmp_path, extra_policy=AUTO)
    main_before = git(repo, "rev-parse", "main")
    e.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", "M1")
    run(e, 240)
    ms = {m["key"]: m for m in q(e, "SELECT * FROM milestones ORDER BY id")}
    assert list(ms) == ["M1", "AUTO-1"]
    assert ms["M1"]["status"] == "COMPLETED" and ms["AUTO-1"]["status"] == "COMPLETED"
    # AUTO-1 started from M1's unmerged integration head, not from main
    m1_head = git(repo, "rev-parse", ms["M1"]["integration_branch"])
    assert ms["AUTO-1"]["base_ref"] == m1_head != main_before
    assert "return a * b" in git(repo, "show", f"{ms['AUTO-1']['integration_branch']}:calc.py")
    # planning happened twice: once proposing AUTO-1, once stopping
    applied = [d["decision_type"] for d in q(e, "SELECT decision_type FROM decisions WHERE status='applied' "
                                              "AND decision_type IN ('PROPOSE_MILESTONE','NO_FURTHER_WORK') ORDER BY id")]
    assert applied == ["PROPOSE_MILESTONE", "NO_FURTHER_WORK"]
    assert q(e, "SELECT COUNT(*) c FROM audit_log WHERE action='milestone.stacked'")[0]["c"] == 1
    assert git(repo, "rev-parse", "main") == main_before


def test_auto_next_off_by_default(engine, demo_project):
    engine.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", "M1")
    run(engine, 120)
    assert not q(engine, "SELECT 1 FROM events WHERE type='PLAN_NEXT_MILESTONE'")
    assert [m["key"] for m in q(engine, "SELECT key FROM milestones")] == ["M1"]


def test_auto_next_daily_cap_and_policy(settings, tmp_path):
    e = make_engine(settings)
    register(e, tmp_path, extra_policy={**AUTO, "auto_next_max_per_day": 0, "hardware_policy": "STRICT_NO_GO"})
    e.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", "M1")
    run(e, 120)
    assert [m["key"] for m in q(e, "SELECT key FROM milestones")] == ["M1"]
    rej = q(e, "SELECT rejection_reason FROM decisions WHERE decision_type='PROPOSE_MILESTONE'")
    assert rej and all("auto_next cap reached" in r["rejection_reason"] for r in rej)


def test_planning_decisions_only_on_planning_events(settings, tmp_path):
    script_agent(settings, "mock-lead", [
        {"rationale": "x", "decisions": [{"type": "PROPOSE_MILESTONE", "key": "SNEAKY",
                                          "goal": "Start a different milestone while this one runs."}]},
    ], tmp_path)
    e = make_engine(settings)
    register(e, tmp_path, extra_policy=AUTO)
    e.submit_goal("demo-calc", "Fix add()", "M1")
    run(e, 20)
    assert [m["key"] for m in q(e, "SELECT key FROM milestones")] == ["M1"]
    r = q(e, "SELECT rejection_reason FROM decisions WHERE decision_type='PROPOSE_MILESTONE'")[0]
    assert "only valid when planning" in r["rejection_reason"]


def test_proposed_goal_still_passes_hardware_policy(settings, tmp_path):
    e = make_engine(settings)
    register(e, tmp_path, extra_policy={**AUTO, "hardware_policy": "STRICT_NO_GO"})
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", "M1")
    run(e, 120)
    # owner-triggered planning with a scripted unsafe proposal
    for a in e.s.agents:
        if a["id"] == "mock-lead":
            script_agent(e.s, "mock-lead", [{"rationale": "x", "decisions": [{
                "type": "PROPOSE_MILESTONE", "key": "HW-1",
                "goal": "Flash the firmware to the camera and run a hardware trial."}]}], tmp_path)
    e.sync_agents()
    e.plan_next_milestone(mid, "owner:test")
    run(e, 30)
    assert [m["key"] for m in q(e, "SELECT key FROM milestones")] == ["M1", "AUTO-1"] or \
        "HW-1" not in [m["key"] for m in q(e, "SELECT key FROM milestones")]
    assert q(e, "SELECT 1 FROM decisions WHERE decision_type='PROPOSE_MILESTONE' AND rejection_reason LIKE "
                "'%denied categories%'")
