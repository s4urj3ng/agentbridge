"""Git worktree management.

Rules enforced here:
- The source clone (`repo_path`) is never reset/cleaned/checked out by us; we
  only fetch and add/remove worktrees. If it is dirty we leave it alone — new
  worktrees are created from a ref, not from the working directory.
- Only AgentBridge-owned branches (agentbridge/*) are created or written.
- No force operations: no `push --force`, no `reset --hard` on shared branches,
  no `branch -D` of branches that are not fully integrated.
- One writable worktree per task, leased in the DB so two agents cannot own it.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..policies.safety import (PolicyViolation, assert_agent_writable_branch, path_matches,
                               scrubbed_env)
from .process import run_cmd


class GitError(Exception):
    pass


def git(repo: Path | str, *args: str, check: bool = True, timeout: float = 120) -> str:
    env = scrubbed_env({"GIT_AUTHOR_NAME": "AgentBridge", "GIT_AUTHOR_EMAIL": "agentbridge@localhost",
                        "GIT_COMMITTER_NAME": "AgentBridge", "GIT_COMMITTER_EMAIL": "agentbridge@localhost"})
    r = run_cmd(["git", *args], cwd=repo, timeout_s=timeout, env=env)
    if check and r.exit_code != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip()[:2000]}")
    return r.stdout.strip()


# Build/cache output never committed or counted as "dirty".
GENERATED_EXCLUDES = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc",
                      ":(exclude,glob)**/.pytest_cache/**", ":(exclude,glob)**/node_modules/**",
                      ":(exclude,glob)**/.mypy_cache/**", ":(exclude,glob)**/.ruff_cache/**"]


def dirty(path: Path | str) -> str:
    return git(path, "status", "--porcelain", "--", ".", *GENERATED_EXCLUDES)


_SAFE_REF = re.compile(r"^[A-Za-z0-9._/\-]+$")


def safe_ref(name: str) -> str:
    if not _SAFE_REF.match(name) or ".." in name or name.startswith("-") or name.endswith(".lock"):
        raise PolicyViolation(f"unsafe ref name: {name!r}")
    return name


def slugify(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "x"


@dataclass
class ChangeSet:
    files: list[dict]        # {path, status, added, deleted}
    diff: str
    base_sha: str
    head_sha: str


class GitWorkspace:
    def __init__(self, repo_path: str | Path, worktrees_root: Path, protected: list[str], remote: str = "origin",
                 overlay: str | Path | None = None):
        self.repo = Path(repo_path).resolve()
        self.root = worktrees_root.resolve()
        self.protected = protected
        self.remote = remote
        self.overlay = Path(overlay).resolve() if overlay else None
        if not (self.repo / ".git").exists():
            raise GitError(f"{self.repo} is not a git repository")

    # ---------------------------------------------------------- remote
    def has_remote(self) -> bool:
        return self.remote in git(self.repo, "remote").split()

    def fetch(self) -> None:
        if self.has_remote():
            git(self.repo, "fetch", "--prune", self.remote, timeout=300)

    def resolve(self, ref: str) -> str:
        return git(self.repo, "rev-parse", "--verify", f"{ref}^{{commit}}")

    def approved_base(self, base_branch: str) -> str:
        """Base sha: remote-tracking branch if a remote exists, else local branch."""
        safe_ref(base_branch)
        if self.has_remote():
            try:
                return self.resolve(f"refs/remotes/{self.remote}/{base_branch}")
            except GitError:
                pass
        return self.resolve(f"refs/heads/{base_branch}")

    def branch_exists(self, branch: str) -> bool:
        return run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=self.repo).exit_code == 0

    def ensure_branch(self, branch: str, start_sha: str) -> None:
        assert_agent_writable_branch(safe_ref(branch), self.protected)
        if not self.branch_exists(branch):
            git(self.repo, "branch", branch, start_sha)

    # ---------------------------------------------------------- worktrees
    def worktree_path(self, project_slug: str, name: str) -> Path:
        return self.root / project_slug / name

    def create_worktree(self, path: Path, branch: str, start_sha: str) -> Path:
        assert_agent_writable_branch(safe_ref(branch), self.protected)
        path = path.resolve()
        if self.root not in path.parents:
            raise PolicyViolation(f"worktree path outside workspace root: {path}")
        if path.exists():
            # Re-use only if it is a registered worktree on the same branch (resume case)
            if self._registered_branch(path) == f"refs/heads/{branch}":
                self.apply_overlay(path)
                return path
            raise GitError(f"worktree path already exists and is not ours: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.branch_exists(branch):
            git(self.repo, "worktree", "add", str(path), branch)
        else:
            git(self.repo, "worktree", "add", "-b", branch, str(path), start_sha)
        self.verify_isolation(path, branch)
        self.apply_overlay(path)
        return path

    def create_detached_worktree(self, path: Path, sha: str) -> Path:
        """Read-only-intent worktree for reviewers (detached HEAD, no branch)."""
        path = path.resolve()
        if self.root not in path.parents:
            raise PolicyViolation(f"worktree path outside workspace root: {path}")
        if path.exists():
            raise GitError(f"worktree path already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "--detach", str(path), sha)
        self.apply_overlay(path)
        return path

    # ---------------------------------------------------------- local overlay
    _EXCLUDE_BEGIN, _EXCLUDE_END = "# >>> agentbridge local overlay", "# <<< agentbridge local overlay"

    def apply_overlay(self, path: Path) -> int:
        """Add the project's local-only files (never in git) to a worktree.

        Read-only files are hard-linked (shared, so keep them read-only); writable files, such as
        prebuilt objects a build may overwrite, are copied per worktree. Symlinks are recreated, and
        anything the checkout already has wins, so tracked files are never replaced. Every overlay path is listed in
        the repository's info/exclude, so these files never show up as changes or get committed.
        """
        if not self.overlay or not self.overlay.is_dir():
            return 0
        added, rels = 0, []
        for rel in self._overlay_files():
            rels.append(rel.as_posix())
            src, dst = self.overlay / rel, path / rel
            if dst.exists() or dst.is_symlink():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                dst.symlink_to(os.readlink(src))
            elif src.stat().st_mode & 0o222:
                shutil.copy2(src, dst)
            else:
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
            added += 1
        self._write_overlay_excludes(rels)
        return added

    def _overlay_files(self) -> list[Path]:
        """Overlay paths relative to its root: files and symlinks (a symlinked directory is one entry)."""
        if not self.overlay or not self.overlay.is_dir():
            return []
        out = []
        for top, dirs, files in os.walk(self.overlay):
            for name in list(dirs):
                if (Path(top) / name).is_symlink():
                    dirs.remove(name)
                    files.append(name)
            out += [(Path(top) / name).relative_to(self.overlay) for name in files]
        return out

    def _write_overlay_excludes(self, rels: list[str]) -> None:
        common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        exclude = (common if common.is_absolute() else self.repo / common) / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        old = exclude.read_text() if exclude.exists() else ""
        keep, inside = [], False
        for line in old.splitlines():
            if line == self._EXCLUDE_BEGIN:
                inside = True
            elif line == self._EXCLUDE_END:
                inside = False
            elif not inside:
                keep.append(line)
        block = [self._EXCLUDE_BEGIN, *("/" + re.sub(r"([\\[*?])", r"\\\1", r) for r in sorted(set(rels))), self._EXCLUDE_END]
        new = "\n".join(keep + block) + "\n"
        if new != old:
            exclude.write_text(new)

    def _registered_branch(self, path: Path) -> str | None:
        cur = None
        for line in git(self.repo, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                cur = Path(line[9:]).resolve()
            elif line.startswith("branch ") and cur == path.resolve():
                return line[7:]
        return None

    def verify_isolation(self, path: Path, branch: str) -> None:
        top = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
        if top != path.resolve():
            raise PolicyViolation(f"worktree toplevel mismatch: {top} != {path}")
        if top == self.repo:
            raise PolicyViolation("worktree resolves to the source clone")
        head = git(path, "symbolic-ref", "HEAD")
        if head != f"refs/heads/{branch}":
            raise PolicyViolation(f"worktree HEAD is {head}, expected {branch}")

    def remove_worktree(self, path: Path) -> None:
        """Remove a worktree only if it is clean (never discard agent work)."""
        if not path.exists():
            git(self.repo, "worktree", "prune")
            return
        if dirty(path):
            raise GitError(f"refusing to remove dirty worktree {path}")
        # only generated files (excluded above) can remain; --force drops just those
        git(self.repo, "worktree", "remove", "--force", str(path))

    # ---------------------------------------------------------- changes
    def commit_all(self, path: Path, message: str) -> str | None:
        """Commit any uncommitted worker changes. Returns new sha or None."""
        if not dirty(path):
            return None
        git(path, "add", "-A", "--", ".", *GENERATED_EXCLUDES)
        # overlay files are never committed, even if an ignore rule went missing or a worker forced them in
        overlay = {r.as_posix() for r in self._overlay_files()}
        swept = [f for f in git(path, "diff", "--cached", "--name-only", "--diff-filter=A").splitlines() if f in overlay]
        if swept:
            git(path, "reset", "-q", "--", *swept)
        if not git(path, "diff", "--cached", "--name-only"):
            return None
        git(path, "commit", "-q", "--no-verify", "-m", message)
        return git(path, "rev-parse", "HEAD")

    def changes(self, path: Path, base_sha: str, max_diff_bytes: int = 400_000) -> ChangeSet:
        head = git(path, "rev-parse", "HEAD")
        files: dict[str, dict] = {}
        for line in git(path, "diff", "--name-status", "--no-renames", base_sha, head).splitlines():
            parts = line.split("\t")
            st, p = parts[0], parts[-1]
            files[p] = {"path": p, "status": st[0], "added": 0, "deleted": 0}
        for line in git(path, "diff", "--numstat", "--no-renames", base_sha, head).splitlines():
            a, d, p = line.split("\t", 2)
            if p in files:
                files[p]["added"] = int(a) if a.isdigit() else 0
                files[p]["deleted"] = int(d) if d.isdigit() else 0
        diff = git(path, "diff", "--no-renames", base_sha, head)
        if len(diff) > max_diff_bytes:
            diff = diff[:max_diff_bytes] + "\n... [diff truncated]\n"
        return ChangeSet(list(files.values()), diff, base_sha, head)

    def unexpected_changes(self, path: Path, cs: ChangeSet, *, forbidden: list[str], allowed: list[str],
                           max_files: int) -> list[str]:
        problems = []
        if len(cs.files) > max_files:
            problems.append(f"too many changed files: {len(cs.files)} > {max_files}")
        for f in cs.files:
            p = f["path"]
            if path_matches(p, forbidden):
                problems.append(f"forbidden path modified: {p}")
            if allowed and not path_matches(p, allowed):
                problems.append(f"path outside task scope: {p}")
            full = path / p
            if full.is_symlink():
                target = full.resolve()
                if path.resolve() not in target.parents:
                    problems.append(f"symlink escapes worktree: {p}")
        # Commits must descend from the base (no history rewrite of the base)
        if run_cmd(["git", "merge-base", "--is-ancestor", cs.base_sha, cs.head_sha], cwd=path).exit_code != 0:
            problems.append("task branch no longer descends from its base (history rewritten)")
        if dirty(path):
            problems.append("worktree has uncommitted changes after collection")
        return problems

    # ---------------------------------------------------------- integration
    def merge_into(self, integration_wt: Path, integration_branch: str, task_branch: str, message: str) -> str:
        """Non-destructive --no-ff merge into an AgentBridge integration branch.

        On conflict the merge is aborted and GitError raised; nothing is reset.
        """
        assert_agent_writable_branch(integration_branch, self.protected)
        if git(integration_wt, "symbolic-ref", "HEAD") != f"refs/heads/{integration_branch}":
            raise PolicyViolation("integration worktree is not on the integration branch")
        if self.is_integrated(task_branch, integration_branch):
            return git(integration_wt, "rev-parse", "HEAD")
        r = run_cmd(["git", "merge", "--no-ff", "--no-edit", "-m", message, task_branch], cwd=integration_wt,
                    env=scrubbed_env({"GIT_AUTHOR_NAME": "AgentBridge", "GIT_AUTHOR_EMAIL": "agentbridge@localhost",
                                      "GIT_COMMITTER_NAME": "AgentBridge", "GIT_COMMITTER_EMAIL": "agentbridge@localhost"}))
        if r.exit_code != 0:
            run_cmd(["git", "merge", "--abort"], cwd=integration_wt)
            raise GitError(f"merge conflict integrating {task_branch}: {r.stdout[-1500:]}")
        return git(integration_wt, "rev-parse", "HEAD")

    def is_integrated(self, branch: str, target: str) -> bool:
        """True if `branch`'s changes are already contained in `target`.

        Handles regular merges (ancestor) and squash merges: if merging branch
        into target yields exactly target's tree, the content is already there.
        This is how stacked PRs whose parents were squash-merged are detected
        without rebasing or force-pushing anything.
        """
        if run_cmd(["git", "merge-base", "--is-ancestor", branch, target], cwd=self.repo).exit_code == 0:
            return True
        r = run_cmd(["git", "merge-tree", "--write-tree", target, branch], cwd=self.repo)
        if r.exit_code != 0:
            return False
        merged_tree = r.stdout.split()[0]
        return merged_tree == git(self.repo, "rev-parse", f"{target}^{{tree}}")

    def push_branch(self, branch: str) -> None:
        """Push an AgentBridge branch (never forced, never protected)."""
        assert_agent_writable_branch(safe_ref(branch), self.protected)
        if not self.has_remote():
            raise GitError("no remote configured")
        git(self.repo, "push", self.remote, f"refs/heads/{branch}:refs/heads/{branch}", timeout=300)
