"""Synthetic demo project: a tiny Python repo with a bug and a missing feature.

No firmware, hardware or real provider is involved; mock agents consume no
subscription quota.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .workers.git_ops import git

CALC = "def add(a, b):\n    return a - b\n"
TEST_ADD = ("import unittest\n\nfrom calc import add\n\n\nclass TestAdd(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")


def create_demo_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if (path / ".git").exists():
        return path
    (path / "calc.py").write_text(CALC)
    (path / "tests").mkdir(exist_ok=True)
    (path / "tests" / "__init__.py").write_text("")
    (path / "tests" / "test_calc.py").write_text(TEST_ADD)
    (path / "README.md").write_text("# calc\n\nSynthetic AgentBridge demo repository.\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial synthetic repo")
    return path


def demo_manifest(repo: Path, slug: str = "demo-calc", *, reviewers: list[str] | None = None,
                  extra_policy: dict | None = None) -> dict:
    return {
        "project": {
            "slug": slug, "name": "Synthetic calc demo", "repo_path": str(repo), "base_branch": "main",
            "tech_lead": "mock-lead", "default_worker": "mock-worker", "workers": ["mock-worker-b"],
            "reviewers": reviewers if reviewers is not None else ["mock-reviewer"], "review_policy": "risk",
        },
        "validation": {"commands": {"unit": ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."]},
                       "default": ["unit"], "timeout_s": 120},
        "policy": {"hardware_policy": "none", "auto_start": True, "draft_pr": "never",
                   "forbidden_paths": [".github/workflows/**", "**/*.pem", "**/.env"], **(extra_policy or {})},
        "charter": "Keep calc.py small, tested and dependency-free.",
        "context": ["Tests use the stdlib unittest runner."],
    }


def write_demo_manifest(repo: Path, out: Path, **kw) -> Path:
    out.write_text(yaml.safe_dump(demo_manifest(repo, **kw), sort_keys=False))
    return out
