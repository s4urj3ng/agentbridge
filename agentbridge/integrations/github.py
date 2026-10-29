"""GitHub integration via the official `gh` CLI plus webhook verification.

- Draft PRs only; AgentBridge never merges in v1.
- Credentials stay with `gh` on the host (`gh auth login`, fine-grained token
  with contents:write + pull_requests:write on the selected repos only).
- Webhooks: HMAC-SHA256 signature check and delivery-id replay protection.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import shutil
import sqlite3
from pathlib import Path

from ..db import now_iso
from ..policies.safety import PolicyViolation, is_protected_branch, redact, scrubbed_env
from ..workers.process import run_cmd


class GitHubError(Exception):
    pass


def repo_slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"github\.com[:/]([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


class GitHubClient:
    def __init__(self, config: dict):
        self.enabled = bool(config.get("enabled"))
        self.gh = config.get("gh_executable", "gh")
        self.step_prs = bool(config.get("step_prs"))   # a draft PR per integrated task, not only per milestone

    def available(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "GitHub integration disabled in config"
        exe = shutil.which(self.gh)
        if not exe:
            return False, "gh CLI not found"
        r = run_cmd([exe, "auth", "status"], cwd="/", timeout_s=30, env=self._env())
        return (r.exit_code == 0, "authenticated" if r.exit_code == 0 else "gh not authenticated")

    def _env(self) -> dict:
        import os
        extra = {k: os.environ[k] for k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR") if k in os.environ}
        return scrubbed_env(extra)

    def create_draft_pr(self, *, repo: str, base: str, head: str, title: str, body: str, workdir: Path,
                        protected: list[str]) -> tuple[str, int | None]:
        if not self.enabled:
            raise GitHubError("GitHub integration disabled")
        if is_protected_branch(head, protected):
            raise PolicyViolation("head branch of a PR may not be a protected branch")
        body_file = workdir / "pr_body.md"
        body_file.write_text(redact(body))
        r = run_cmd([self.gh, "pr", "create", "--draft", "--repo", repo, "--base", base, "--head", head,
                     "--title", title[:250], "--body-file", str(body_file)], cwd=workdir, timeout_s=120,
                    env=self._env())
        if r.exit_code != 0:
            raise GitHubError(redact(r.stderr.strip())[:1000])
        url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        m = re.search(r"/pull/(\d+)", url)
        return url, int(m.group(1)) if m else None


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def record_delivery(conn: sqlite3.Connection, delivery_id: str, event_type: str) -> bool:
    """Returns False if this delivery was already processed (replay)."""
    cur = conn.execute("INSERT OR IGNORE INTO webhook_deliveries (delivery_id, event_type, received_at) VALUES (?,?,?)",
                       (delivery_id, event_type, now_iso()))
    return cur.rowcount == 1
