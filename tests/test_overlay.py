"""project.local_overlay: local-only files (ROM dump, SDK tree, build scripts) in every worktree, never committed."""
from __future__ import annotations

import os

from agentbridge.demo import create_demo_repo
from agentbridge.workers.git_ops import GitWorkspace, git

from .conftest import make_engine, q, register, run


def _overlay(tmp_path):
    ov = tmp_path / "overlay"
    (ov / "firmware" / "dumps").mkdir(parents=True)
    (ov / "firmware" / "dumps" / "ROM[1].BIN").write_bytes(b"\x00\x01")
    (ov / "tools").mkdir()
    (ov / "tools" / "build_local.py").write_text("print('local')\n")
    (ov / "calc.py").write_text("TRACKED FILE MUST WIN\n")
    (ov / "build").mkdir()
    (ov / "build" / "prebuilt.o").write_bytes(b"obj")
    (tmp_path / "toolchain").mkdir()
    (ov / "toolchains").symlink_to(tmp_path / "toolchain")
    for f in ov.rglob("*"):
        if f.is_file() and not f.is_symlink() and f.suffix != ".o":
            f.chmod(0o444)
    return ov


def test_overlay_files_present_but_invisible_to_git(tmp_path):
    repo = create_demo_repo(tmp_path / "repo")
    ws = GitWorkspace(repo, tmp_path / "wt", ["main"], overlay=_overlay(tmp_path))
    base = git(repo, "rev-parse", "main")
    path = ws.create_worktree(tmp_path / "wt" / "p" / "t1", "agentbridge/p/m/t1", base)
    assert (path / "firmware" / "dumps" / "ROM[1].BIN").read_bytes() == b"\x00\x01"
    assert (path / "tools" / "build_local.py").stat().st_ino == (tmp_path / "overlay" / "tools" / "build_local.py").stat().st_ino
    assert os.path.islink(path / "toolchains")
    # writable overlay files are private copies a build can overwrite
    (path / "build" / "prebuilt.o").write_bytes(b"rebuilt")
    assert (tmp_path / "overlay" / "build" / "prebuilt.o").read_bytes() == b"obj"
    assert "TRACKED FILE MUST WIN" not in (path / "calc.py").read_text()
    assert git(path, "status", "--porcelain", "--untracked-files=all") == ""
    (path / "notes.md").write_text("real work\n")
    assert ws.commit_all(path, "work")
    assert git(path, "show", "--name-only", "--format=", "HEAD").split() == ["notes.md"]
    # a reviewer's detached worktree gets the same files; re-applying does not duplicate excludes
    rpath = ws.create_detached_worktree(tmp_path / "wt" / "p" / "r1", base)
    assert (rpath / "tools" / "build_local.py").exists()
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.count("# >>> agentbridge local overlay") == 1 and "/firmware/dumps/ROM\\[1].BIN" in exclude


def test_overlay_reaches_workers_and_never_lands_in_commits(settings, tmp_path):
    e = make_engine(settings)
    register(e, tmp_path, project_overrides={"local_overlay": str(_overlay(tmp_path))})
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", "OV1")
    run(e, 120)
    ms = q(e, "SELECT * FROM milestones WHERE id=?", mid)[0]
    assert ms["status"] == "COMPLETED"
    repo = q(e, "SELECT repo_path FROM projects")[0]["repo_path"]
    files = git(repo, "diff", "--name-only", f"{ms['base_ref']}..{ms['integration_branch']}").split()
    assert files and not [f for f in files if f.startswith(("firmware/", "tools/", "toolchains"))]
    wt = q(e, "SELECT worktree_path FROM tasks WHERE milestone_id=? LIMIT 1", mid)[0]["worktree_path"]
    assert wt is None or not os.path.exists(wt) or os.path.exists(os.path.join(wt, "tools", "build_local.py"))
