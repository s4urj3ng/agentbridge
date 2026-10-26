"""Git safety: isolation, dirty user checkouts, squash-merged stacks, no destructive ops."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentbridge.demo import create_demo_repo
from agentbridge.policies.safety import PolicyViolation
from agentbridge.workers.git_ops import GitError, GitWorkspace, git

from .conftest import q, register, run


def _ws(tmp_path) -> tuple[GitWorkspace, Path]:
    repo = create_demo_repo(tmp_path / "repo")
    root = tmp_path / "wts"
    root.mkdir()
    return GitWorkspace(repo, root, ["main"]), repo


def test_worktree_isolation(tmp_path):
    ws, repo = _ws(tmp_path)
    base = ws.resolve("main")
    p = ws.create_worktree(tmp_path / "wts" / "a", "agentbridge/p/m/a", base)
    ws.verify_isolation(p, "agentbridge/p/m/a")
    (p / "new.txt").write_text("x")
    assert not (repo / "new.txt").exists()
    with pytest.raises(PolicyViolation):
        ws.create_worktree(tmp_path / "outside", "agentbridge/p/m/b", base)
    with pytest.raises(GitError):  # same branch cannot be checked out writable twice
        ws.create_worktree(tmp_path / "wts" / "dup", "agentbridge/p/m/a", base)


def test_dirty_user_checkout_is_never_touched(engine, tmp_path):
    repo = register(engine, tmp_path)
    (repo / "calc.py").write_text("# user's uncommitted work in progress\n")
    (repo / "scratch.txt").write_text("untracked user file")
    status_before = git(repo, "status", "--porcelain")
    engine.submit_goal("demo-calc", "Fix add() and implement mul()", "G1")
    run(engine, 120)
    assert git(repo, "status", "--porcelain") == status_before
    assert (repo / "calc.py").read_text() == "# user's uncommitted work in progress\n"
    assert q(engine, "SELECT status FROM milestones")[0]["status"] == "COMPLETED"


def test_remove_worktree_refuses_dirty(tmp_path):
    ws, repo = _ws(tmp_path)
    p = ws.create_worktree(tmp_path / "wts" / "a", "agentbridge/p/m/a", ws.resolve("main"))
    (p / "calc.py").write_text("changed")
    with pytest.raises(GitError):
        ws.remove_worktree(p)
    assert p.exists()


def test_squash_merged_stacked_branch_detected(tmp_path):
    """Branch B is stacked on A. A is squash-merged into main. B's A-part must be recognised as integrated."""
    ws, repo = _ws(tmp_path)
    git(repo, "checkout", "-q", "-b", "agentbridge/p/a")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "a1")
    (repo / "a.txt").write_text("a\naa\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "a2")
    git(repo, "checkout", "-q", "-b", "agentbridge/p/b")
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "b1")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--squash", "agentbridge/p/a")
    git(repo, "commit", "-q", "-m", "squash A")
    assert git(repo, "merge-base", "--is-ancestor", "agentbridge/p/a", "main", check=False) == ""  # not ancestor
    assert ws.is_integrated("agentbridge/p/a", "main")       # squash detected via tree comparison
    assert not ws.is_integrated("agentbridge/p/b", "main")   # B still has unmerged work
    # B merges cleanly onto the squashed main without rebasing or force-pushing
    assert git(repo, "merge-tree", "--write-tree", "main", "agentbridge/p/b")


def test_merge_conflict_aborts_without_reset(tmp_path):
    ws, repo = _ws(tmp_path)
    base = ws.resolve("main")
    integ = ws.create_worktree(tmp_path / "wts" / "integ", "agentbridge/p/m/integration", base)
    t1 = ws.create_worktree(tmp_path / "wts" / "t1", "agentbridge/p/m/t1", base)
    t2 = ws.create_worktree(tmp_path / "wts" / "t2", "agentbridge/p/m/t2", base)
    (t1 / "calc.py").write_text("one\n"); ws.commit_all(t1, "t1")
    (t2 / "calc.py").write_text("two\n"); ws.commit_all(t2, "t2")
    ws.merge_into(integ, "agentbridge/p/m/integration", "agentbridge/p/m/t1", "m1")
    head = git(integ, "rev-parse", "HEAD")
    with pytest.raises(GitError):
        ws.merge_into(integ, "agentbridge/p/m/integration", "agentbridge/p/m/t2", "m2")
    assert git(integ, "rev-parse", "HEAD") == head
    assert git(integ, "status", "--porcelain") == ""
    assert git(t2, "rev-parse", "HEAD")  # t2's work still exists on its branch
