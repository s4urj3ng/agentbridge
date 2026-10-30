"""policy.self_merge: MERGE_MILESTONE from the Tech Lead merges a completed milestone's PR into the base."""
from __future__ import annotations

import subprocess

from agentbridge.db import row, tx
from agentbridge.orchestrator.decisions import parse_envelope
from agentbridge.workers.git_ops import git

from .conftest import make_engine, q, register, run

GOAL = "Fix add() and implement mul() in calc.py with tests"


def _setup(settings, tmp_path, **policy):
    settings.github = {"enabled": True}
    e = make_engine(settings)
    repo = register(e, tmp_path, extra_policy={"autonomous": True, "draft_pr": "never", **policy},
                    project_overrides={"remote_url": "https://github.com/me/demo-calc"})
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    calls = []

    def create_draft_pr(**kw):
        calls.append(("create", kw["head"]))
        return f"https://github.com/me/demo-calc/pull/{len(calls)}", len(calls)

    def merge_pr(**kw):
        calls.append(("merge", kw["number"], kw["head_sha"]))
        git(repo, "push", "-q", "origin", f"{kw['head_sha']}:refs/heads/main")   # stand-in for GitHub's merge

    e.github.create_draft_pr, e.github.merge_pr = create_draft_pr, merge_pr
    return e, repo, remote, calls


def _plan(e, mid, *keys):
    with tx(e.conn):
        eid = e.conn.execute("INSERT INTO events (type, project_id, milestone_id, payload_json, status, created_at) "
                             "SELECT 'PLAN_NEXT_MILESTONE', project_id, id, '{}', 'DONE', '2026-01-01' FROM milestones "
                             "WHERE id=?", (mid,)).lastrowid
    env = parse_envelope({"rationale": "r", "decisions": [
        {"type": "MERGE_MILESTONE", "milestone_key": k, "reason": "reviewed and validated"} for k in keys]})
    return e.apply_decisions(env, row(e.conn, "SELECT * FROM events WHERE id=?", eid),
                             row(e.conn, "SELECT * FROM projects"), row(e.conn, "SELECT * FROM milestones WHERE id=?", mid),
                             None)


def test_tech_lead_merges_stacked_milestones_in_order(settings, tmp_path):
    e, repo, remote, calls = _setup(settings, tmp_path, self_approve=True, self_merge=True)
    m1 = e.submit_goal("demo-calc", GOAL, "M1")
    run(e, 120)
    m2 = e.submit_goal("demo-calc", "Write short design notes for the next iteration of the project.", "AUTO-2")
    run(e, 120)
    ms = {r["key"]: r for r in q(e, "SELECT * FROM milestones")}
    assert ms["AUTO-2"]["base_ref"] == git(repo, "rev-parse", ms["M1"]["integration_branch"])   # stacked
    assert e._unmerged_milestones(row(e.conn, "SELECT * FROM projects"), e.manifest(1)) == ["M1", "AUTO-2"]

    applied, rejected = _plan(e, m2, "M1", "AUTO-2")
    assert applied == 2 and not rejected
    run(e, 60)
    assert [c[0] for c in calls] == ["create", "merge", "create", "merge"]
    assert git(remote, "rev-parse", "main") == git(repo, "rev-parse", ms["AUTO-2"]["integration_branch"])
    assert q(e, "SELECT COUNT(*) c FROM audit_log WHERE action='github.merged'")[0]["c"] == 2
    assert {a["resolved_by"] for a in q(e, "SELECT resolved_by FROM approval_requests "
                                           "WHERE action='merge_milestone'")} == {"tech_lead:self"}
    assert e._unmerged_milestones(row(e.conn, "SELECT * FROM projects"), e.manifest(1)) == []


def test_out_of_order_merge_refused_and_owner_gate_without_self_merge(settings, tmp_path):
    e, repo, remote, calls = _setup(settings, tmp_path, self_approve=True, self_merge=True)
    e.submit_goal("demo-calc", GOAL, "M1")
    run(e, 120)
    m2 = e.submit_goal("demo-calc", "Write short design notes for the next iteration of the project.", "AUTO-2")
    run(e, 120)
    main_before = git(remote, "rev-parse", "main")
    _plan(e, m2, "AUTO-2")
    run(e, 60)
    assert git(remote, "rev-parse", "main") == main_before and not [c for c in calls if c[0] == "merge"]
    failed = q(e, "SELECT detail_json FROM audit_log WHERE action='system_event.failed'")
    assert failed and "merge M1 before AUTO-2" in failed[-1]["detail_json"]
    # a completed milestone that does not exist / is not complete is rejected at decision time
    _, rejected = _plan(e, m2, "NOPE")
    assert rejected and "not a completed milestone" in rejected[0]


def test_merge_waits_for_owner_without_self_merge(settings, tmp_path):
    e, repo, remote, calls = _setup(settings, tmp_path, self_approve=True)
    m1 = e.submit_goal("demo-calc", GOAL, "M1")
    run(e, 120)
    _plan(e, m1, "M1")
    run(e, 30)
    assert q(e, "SELECT status FROM approval_requests WHERE action='merge_milestone'") == [{"status": "PENDING"}]
    assert not calls


def test_transient_github_error_is_retried(settings, tmp_path):
    from agentbridge.integrations.github import GitHubError, is_transient
    e, repo, remote, calls = _setup(settings, tmp_path, self_approve=True, self_merge=True)
    m1 = e.submit_goal("demo-calc", GOAL, "M1")
    run(e, 120)
    real, fails = e.github.create_draft_pr, []

    def flaky(**kw):
        if len(fails) < 2:
            fails.append(1)
            raise GitHubError("pull request create failed: GraphQL: Something went wrong while executing your query")
        return real(**kw)

    e.github.create_draft_pr = flaky
    _plan(e, m1, "M1")
    for _ in range(3):
        run(e, 30)
        e.conn.execute("UPDATE events SET not_before=NULL WHERE status='DEFERRED'")   # skip the backoff
    assert len(fails) == 2 and [c[0] for c in calls] == ["create", "merge"]
    assert q(e, "SELECT COUNT(*) c FROM audit_log WHERE action='system_event.retry'")[0]["c"] == 2
    assert not q(e, "SELECT 1 FROM audit_log WHERE action='system_event.failed'")
    # a permanent error still fails at once
    assert not is_transient(GitHubError("no commits between"))
