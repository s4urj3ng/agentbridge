"""Step PRs: every integrated task gets its own draft PR (task branch -> integration branch) on GitHub."""
from __future__ import annotations

import subprocess

from agentbridge.integrations.github import GitHubError
from agentbridge.workers.git_ops import git

from .conftest import make_engine, q, register, run


def _setup(settings, tmp_path, fail=False):
    settings.github = {"enabled": True, "step_prs": True}
    e = make_engine(settings)
    repo = register(e, tmp_path, project_overrides={"remote_url": "https://github.com/me/demo-calc"})
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    calls = []

    def create_draft_pr(**kw):
        if fail:
            raise GitHubError("HTTP 401: Bad credentials")
        heads = git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        calls.append(kw | {"remote_heads": heads.split()})
        return f"https://github.com/me/demo-calc/pull/{len(calls)}", len(calls)

    e.github.create_draft_pr = create_draft_pr
    return e, remote, calls


def test_each_integrated_task_opens_a_draft_step_pr(settings, tmp_path):
    e, remote, calls = _setup(settings, tmp_path)
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "S1")
    run(e, 120)
    tasks = {t["key"]: t for t in q(e, "SELECT * FROM tasks WHERE milestone_id=?", mid)}
    assert all(t["state"] == "COMPLETED" for t in tasks.values())
    ms = q(e, "SELECT * FROM milestones WHERE id=?", mid)[0]
    assert [c["head"] for c in calls] == [tasks["fix-add"]["branch"], tasks["impl-mul"]["branch"]]
    for c in calls:
        assert c["base"] == ms["integration_branch"] and c["repo"] == "me/demo-calc"
        assert c["head"] in c["remote_heads"] and ms["integration_branch"] in c["remote_heads"]  # pushed first
        assert "S1 · " in c["title"] and "### Reviews" in c["body"]
    assert {t["pr_number"] for t in tasks.values()} == {1, 2}
    # the integration branch on the remote has every step (so GitHub shows the step PRs as merged into it)
    assert git(remote, "rev-parse", ms["integration_branch"]) == git(e.workspace(
        q(e, "SELECT * FROM projects")[0]).repo, "rev-parse", ms["integration_branch"])
    assert "main" not in git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def test_github_failure_never_blocks_integration(settings, tmp_path):
    e, remote, calls = _setup(settings, tmp_path, fail=True)
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "S2")
    run(e, 120)
    assert all(t["state"] == "COMPLETED" for t in q(e, "SELECT state FROM tasks WHERE milestone_id=?", mid))
    failed = q(e, "SELECT detail_json FROM audit_log WHERE action='github.step_pr_failed'")
    assert len(failed) == 2 and "Bad credentials" in failed[0]["detail_json"]


def test_milestone_pr_opened_early_is_not_duplicated(settings, tmp_path):
    import asyncio
    e, remote, calls = _setup(settings, tmp_path)
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "S3")
    run(e, 120)
    e.conn.execute("UPDATE milestones SET pr_url='https://github.com/me/demo-calc/pull/9', pr_number=9 WHERE id=?",
                   (mid,))
    n = len(calls)
    asyncio.run(e._create_draft_pr(mid))
    assert len(calls) == n and q(e, "SELECT COUNT(*) c FROM audit_log WHERE action='github.draft_pr_updated'")[0]["c"] == 1
