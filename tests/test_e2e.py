"""Acceptance tests 1-8 and 14: the full mock-provider loop, real subprocesses and git."""
from __future__ import annotations

import subprocess
from pathlib import Path

from agentbridge.workers.git_ops import git

from .conftest import q, run


def test_full_loop_mock_providers(engine, demo_project: Path):
    repo = demo_project
    main_before = git(repo, "rev-parse", "main")

    # 1. submit a goal
    mid = engine.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", "M1")
    run(engine, 120)

    tasks = {t["key"]: t for t in q(engine, "SELECT * FROM tasks WHERE milestone_id=?", mid)}
    # 2. Tech Lead produced valid structured tasks (with a dependency)
    assert set(tasks) == {"fix-add", "impl-mul"}
    deps = q(engine, "SELECT * FROM task_dependencies")
    assert deps == [{"task_id": tasks["impl-mul"]["id"], "depends_on_id": tasks["fix-add"]["id"]}]
    assert q(engine, "SELECT COUNT(*) c FROM decisions WHERE status='applied' AND decision_type='CREATE_TASKS'")[0]["c"] == 1

    # 3. workers executed in separate worktrees on separate branches (not the source checkout)
    for t in tasks.values():
        assert t["branch"].startswith("agentbridge/demo-calc/m1/")
        assert Path(t["worktree_path"]).resolve() != repo.resolve()
        assert str(engine.s.workspaces_dir) in t["worktree_path"]
    assert tasks["fix-add"]["assignee"] == "mock-worker" and tasks["impl-mul"]["assignee"] == "mock-worker-b"

    # 4. worker output captured as artifacts
    arts = q(engine, "SELECT kind, path FROM artifacts WHERE task_id=?", tasks["fix-add"]["id"])
    kinds = {a["kind"] for a in arts}
    assert {"stdout", "stderr", "prompt", "diff", "validation_log"} <= kinds
    stdout = [a for a in arts if a["kind"] == "stdout"][0]
    assert "structured_output" in Path(stdout["path"]).read_text()

    # 5. deterministic tests ran; the first impl-mul attempt was buggy and failed validation
    vr = q(engine, "SELECT passed, results_json FROM validation_runs WHERE task_id=? ORDER BY id", tasks["impl-mul"]["id"])
    assert [v["passed"] for v in vr] == [0, 1]
    assert tasks["impl-mul"]["validation_retries"] == 1  # automatic retry without a Tech Lead call

    # 6. completion triggered Tech Lead invocations; 7. TL selected follow-up actions
    tl_events = [e["type"] for e in q(engine, "SELECT type FROM events ORDER BY id")]
    assert tl_events.count("TASK_READY_FOR_REVIEW") == 3
    assert "REVIEW_COMPLETED" in tl_events and "MILESTONE_READY" in tl_events
    dtypes = [d["decision_type"] for d in q(engine, "SELECT decision_type FROM decisions WHERE status='applied' ORDER BY id")]
    assert dtypes == ["CREATE_TASKS", "REQUEST_REVISION", "APPROVE_TASK", "REQUEST_REVIEW", "APPROVE_TASK",
                      "COMPLETE_MILESTONE"]
    tl_calls = q(engine, "SELECT COUNT(*) c FROM execution_attempts WHERE purpose='tech_lead'")[0]["c"]
    assert tl_calls == 6  # event-driven: exactly one per meaningful event, no polling

    # 8. revision created automatically (TL asked fix-add for a docstring)
    assert tasks["fix-add"]["revision_count"] == 1
    assert "docstring" in tasks["fix-add"]["feedback"]

    # independent review by a different identity than the author
    rv = q(engine, "SELECT reviewer, verdict FROM reviews WHERE task_id=? AND source='agent'", tasks["impl-mul"]["id"])
    assert rv == [{"reviewer": "mock-reviewer", "verdict": "approve"}]

    # end state: both completed, milestone completed, integration branch has the work, main untouched
    assert all(t["state"] == "COMPLETED" for t in tasks.values())
    ms = q(engine, "SELECT * FROM milestones WHERE id=?", mid)[0]
    assert ms["status"] == "COMPLETED"
    calc = git(repo, "show", f"{ms['integration_branch']}:calc.py")
    assert "return a + b" in calc and "return a * b" in calc and '"""Return the sum' in calc
    assert git(repo, "rev-parse", "main") == main_before
    r = subprocess.run(["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       cwd=engine.s.workspaces_dir / "demo-calc" / "m1-integration", capture_output=True)
    assert r.returncode == 0

    # 14. mock mode consumed no subscription provider at all
    providers = {a["provider"] for a in q(engine, "SELECT DISTINCT a.provider FROM execution_attempts e "
                                                  "JOIN agents a ON a.id=e.agent_id")}
    assert providers == {"mock"}


def test_state_transitions_are_recorded_and_valid(engine, demo_project):
    from agentbridge.orchestrator.states import can_transition
    engine.submit_goal("demo-calc", "Fix add() and implement mul()", "M2")
    run(engine, 120)
    for tr in q(engine, "SELECT from_state, to_state FROM task_transitions WHERE from_state != '-'"):
        assert can_transition(tr["from_state"], tr["to_state"]), tr
    # no task ever jumped from RUNNING to COMPLETED/APPROVED
    assert not q(engine, "SELECT * FROM task_transitions WHERE from_state='RUNNING' AND to_state IN ('COMPLETED','APPROVED')")


def test_auto_start_false_requires_owner_approval(engine, tmp_path):
    from .conftest import register
    register(engine, tmp_path, extra_policy={"auto_start": False})
    mid = engine.submit_goal("demo-calc", "Fix add()", "M3")
    run(engine, 30)
    assert q(engine, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "AWAITING_APPROVAL"
    assert q(engine, "SELECT COUNT(*) c FROM execution_attempts")[0]["c"] == 0  # nothing ran
    ap = q(engine, "SELECT * FROM approval_requests WHERE action='start_milestone'")[0]
    engine.resolve_approval(ap["id"], True, "owner:test")
    run(engine, 120)
    assert q(engine, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
