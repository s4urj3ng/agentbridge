"""AgentBridge orchestration engine.

One engine process (the *host worker*) owns execution: it claims events,
invokes the Tech Lead on meaningful events only, dispatches ready tasks to
worker CLIs in isolated worktrees, validates results deterministically and
integrates approved work into an AgentBridge-owned milestone branch.

The API/dashboard process never executes agents; it only writes rows/events
that this engine picks up. All state lives in SQLite, so the loop is
resumable after a crash or reboot (see `recover()`).
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter, AgentResult, ExecRequest
from ..adapters.registry import make_adapter
from ..config import Settings
from ..db import audit, connect, jdump, jload, migrate, now_iso, parse_ts, row, rows, tx
from ..integrations.github import GitHubClient, GitHubError, repo_slug_from_url
from ..policies.manifest import Manifest
from ..policies.safety import (PolicyViolation, hardware_no_go_hits, is_protected_branch, redact)
from ..workers import process
from ..workers.git_ops import GitError, GitWorkspace, slugify
from ..workers.validation import run_validation
from . import events as evq
from . import quota
from .context import build_review_prompt, build_tech_lead_prompt, build_worker_prompt
from .decisions import (DecisionEnvelope, MalformedDecision, ReviewResult, WorkerResult, envelope_json_schema,
                        parse_envelope, review_result_schema, worker_result_schema)
from .states import TERMINAL, InvalidTransition, StaleTransition, TaskState as S, transition

log = logging.getLogger("agentbridge.engine")

FAIL_OUTCOMES = {"failed", "timeout", "lost", "malformed", "unavailable"}


class EngineLocked(Exception):
    pass


class Engine:
    def __init__(self, settings: Settings, conn: sqlite3.Connection | None = None):
        self.s = settings
        settings.ensure_dirs()
        self.conn = conn or connect(settings.db_path)
        migrate(self.conn)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self.jobs: dict[str, asyncio.Task] = {}
        self._lock_fh = None
        self._recovered = False
        self._adoptions: list[sqlite3.Row] = []
        self.github = GitHubClient(settings.github)

    # ================================================================ setup
    def acquire_lock(self) -> None:
        """Only one engine may execute agents against a data dir."""
        self._lock_fh = open(self.s.data_dir / "engine.lock", "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EngineLocked(f"another engine holds {self.s.data_dir / 'engine.lock'}") from None
        self._lock_fh.write(self.worker_id)
        self._lock_fh.flush()

    def release_lock(self) -> None:
        if self._lock_fh:
            fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
            self._lock_fh.close()
            self._lock_fh = None

    def sync_agents(self) -> None:
        """Upsert identities from config. Runtime state (paused, cooldown) is preserved."""
        with tx(self.conn):
            for a in self.s.agents:
                cfg = {k: v for k, v in a.items() if k not in (
                    "id", "provider", "roles", "enabled", "max_concurrency", "daily_invocation_limit",
                    "weekly_invocation_limit", "reserved_review_invocations")}
                if a.get("provider") == "mock":
                    cfg.setdefault("state_dir", str(self.s.data_dir / "mock-state"))
                existing = row(self.conn, "SELECT id FROM agents WHERE id=?", a["id"])
                vals = (a["provider"], jdump(a.get("roles", ["worker"])), int(a.get("max_concurrency", 1)),
                        a.get("daily_invocation_limit"), a.get("weekly_invocation_limit"),
                        int(a.get("reserved_review_invocations", 0)), jdump(cfg), now_iso())
                if existing:
                    self.conn.execute("UPDATE agents SET provider=?, roles_json=?, max_concurrency=?, "
                                      "daily_invocation_limit=?, weekly_invocation_limit=?, "
                                      "reserved_review_invocations=?, config_json=?, updated_at=? WHERE id=?",
                                      (*vals, a["id"]))
                    if a.get("enabled") is False:   # an explicit disable in config always wins
                        self.conn.execute("UPDATE agents SET enabled=0 WHERE id=?", (a["id"],))
                else:
                    self.conn.execute("INSERT INTO agents (provider, roles_json, max_concurrency, daily_invocation_limit,"
                                      " weekly_invocation_limit, reserved_review_invocations, config_json, updated_at,"
                                      " id, enabled) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                      (*vals, a["id"], int(a.get("enabled", True))))

    def adapter(self, agent_id: str) -> AgentAdapter:
        a = row(self.conn, "SELECT provider, config_json FROM agents WHERE id=?", agent_id)
        if a is None:
            raise ValueError(f"unknown agent {agent_id}")
        return make_adapter(a["provider"], agent_id, jload(a["config_json"], {}))

    def _config_problem(self, agent_id: str) -> str | None:
        """Static adapter-config refusal (e.g. a model outside the allowed quota pool)."""
        ad = self.adapter(agent_id)
        check = getattr(ad, "model_allowed", None)
        if check:
            ok, why = check(ad.config.get("model"))
            return None if ok else why
        return None

    def detect_agents(self) -> dict[str, dict]:
        out = {}
        for a in rows(self.conn, "SELECT id FROM agents ORDER BY id"):
            ad = self.adapter(a["id"])
            cap = ad.detect().to_dict()
            auth = ad.validate_auth()
            cap["auth"] = {"state": auth.state, "method": auth.method, "detail": redact(auth.detail)}
            with tx(self.conn):
                self.conn.execute("UPDATE agents SET capability_json=?, last_detected_at=? WHERE id=?",
                                  (jdump(cap), now_iso(), a["id"]))
            out[a["id"]] = cap
        return out

    def register_project(self, manifest: Manifest) -> int:
        p = manifest.project
        GitWorkspace(p.repo_path, self.s.workspaces_dir, self._protected(manifest), p.remote_name)  # validates repo
        mjson = manifest.model_dump_json()
        with tx(self.conn):
            ex = row(self.conn, "SELECT id FROM projects WHERE slug=?", p.slug)
            if ex:
                self.conn.execute("UPDATE projects SET name=?, repo_path=?, remote_url=?, remote_name=?, base_branch=?, "
                                  "manifest_json=? WHERE id=?",
                                  (p.name, p.repo_path, p.remote_url, p.remote_name, p.base_branch, mjson, ex["id"]))
                pid = ex["id"]
            else:
                pid = self.conn.execute(
                    "INSERT INTO projects (slug, name, repo_path, remote_url, remote_name, base_branch, manifest_json, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (p.slug, p.name, p.repo_path, p.remote_url, p.remote_name, p.base_branch, mjson, now_iso())).lastrowid
                if manifest.charter:
                    self._add_context(pid, "charter", manifest.charter)
                for c in manifest.context:
                    self._add_context(pid, "verified_fact", c)
                for m in manifest.completed_milestones:
                    self._add_context(pid, "verified_fact", f"Milestone {m} completed before AgentBridge onboarding.")
                cats = manifest.policy.denied_categories()
                if cats:
                    self._add_context(pid, "constraint", "Hardware policy: denied categories " + ", ".join(cats))
            audit(self.conn, "owner", "project.register", project_id=pid, slug=p.slug)
        return pid

    def _add_context(self, pid: int, kind: str, content: str) -> None:
        self.conn.execute("INSERT INTO project_context (project_id, kind, content, created_at) VALUES (?,?,?,?)",
                          (pid, kind, content, now_iso()))

    def manifest(self, project_id: int) -> Manifest:
        r = row(self.conn, "SELECT manifest_json FROM projects WHERE id=?", project_id)
        return Manifest.model_validate_json(r["manifest_json"])

    def _protected(self, m: Manifest) -> list[str]:
        return list(dict.fromkeys(self.s.protected_branches + m.policy.protected_branches + [m.project.base_branch]))

    def _limits(self, m: Manifest) -> dict[str, int]:
        lim = dict(vars(self.s.limits))
        for k, v in m.limits.model_dump().items():
            if v is not None:
                lim[k] = v
        return lim

    def workspace(self, project: sqlite3.Row, m: Manifest | None = None) -> GitWorkspace:
        m = m or self.manifest(project["id"])
        return GitWorkspace(project["repo_path"], self.s.workspaces_dir, self._protected(m), project["remote_name"])

    # ================================================================ owner operations (API-safe: DB only)
    def submit_goal(self, project_slug: str, goal: str, milestone_key: str | None = None, actor: str = "owner") -> int:
        p = row(self.conn, "SELECT * FROM projects WHERE slug=?", project_slug)
        if p is None:
            raise ValueError(f"unknown project {project_slug}")
        m = self.manifest(p["id"])
        hits = hardware_no_go_hits(goal, m.policy.denied_categories())
        if hits:
            raise PolicyViolation(f"goal touches denied categories {hits} (project hardware policy)")
        key = milestone_key or f"M{int(time.time())}"
        with tx(self.conn):
            status = "ACTIVE" if m.policy.auto_start else "AWAITING_APPROVAL"
            mid = self.conn.execute("INSERT INTO milestones (project_id, key, goal, status, created_at) VALUES (?,?,?,?,?)",
                                    (p["id"], key, goal, status, now_iso())).lastrowid
            self._add_context(p["id"], "goal", f"[{key}] {goal[:500]}")
            if m.policy.auto_start:
                evq.emit(self.conn, "GOAL_SUBMITTED", project_id=p["id"], milestone_id=mid,
                         payload={"goal": goal}, dedupe_key=f"goal:{mid}")
            else:
                self._request_approval(p["id"], "start_milestone", f"Start milestone {key}: {goal[:300]}",
                                       actor, milestone_id=mid, dedupe_key=f"start:{mid}")
            audit(self.conn, actor, "goal.submit", project_id=p["id"], milestone=key)
        return mid

    def _request_approval(self, project_id: int, action: str, reason: str, requested_by: str, *,
                          milestone_id: int | None = None, task_id: int | None = None,
                          evidence: dict | None = None, dedupe_key: str | None = None) -> int | None:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO approval_requests (project_id, milestone_id, task_id, action, reason, evidence_json, "
            "requested_by, dedupe_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (project_id, milestone_id, task_id, action, redact(reason)[:3000], jdump(evidence or {}), requested_by,
             dedupe_key, now_iso()))
        if cur.rowcount:
            audit(self.conn, requested_by, "approval.request", project_id=project_id, task_id=task_id, action=action)
            return cur.lastrowid
        return None

    def resolve_approval(self, approval_id: int, approve: bool, actor: str, note: str = "") -> None:
        with tx(self.conn):
            a = row(self.conn, "SELECT * FROM approval_requests WHERE id=?", approval_id)
            if a is None or a["status"] != "PENDING":
                raise ValueError("approval not pending")
            if approve and a["action"] == "merge_protected":
                raise PolicyViolation("merging into protected branches is not supported in v1")
            self.conn.execute("UPDATE approval_requests SET status=?, resolved_by=?, resolution_note=?, resolved_at=? "
                              "WHERE id=?", ("APPROVED" if approve else "REJECTED", actor, note[:2000], now_iso(),
                                             approval_id))
            evq.emit(self.conn, "APPROVAL_RESOLVED", project_id=a["project_id"], milestone_id=a["milestone_id"],
                     task_id=a["task_id"], payload={"approval_id": approval_id, "approved": approve,
                                                    "action": a["action"], "note": note[:1000]},
                     dedupe_key=f"approval:{approval_id}")
            audit(self.conn, actor, "approval.resolve", project_id=a["project_id"], task_id=a["task_id"],
                  approval_id=approval_id, approved=approve)

    def cancel_task(self, task_id: int, actor: str) -> None:
        transition(self.conn, task_id, S.CANCELLED, actor=actor, reason="cancelled by owner")

    def request_merge(self, project_slug: str, source: str, target: str, actor: str) -> None:
        """v1 policy: no automatic merges into protected branches, ever."""
        p = row(self.conn, "SELECT * FROM projects WHERE slug=?", project_slug)
        if p is None:
            raise ValueError("unknown project")
        m = self.manifest(p["id"])
        with tx(self.conn):
            audit(self.conn, actor, "merge.denied", project_id=p["id"], source=source, target=target)
        if is_protected_branch(target, self._protected(m)):
            raise PolicyViolation(f"merge into protected branch {target!r} is blocked (v1 policy)")
        raise PolicyViolation("direct merges are not supported in v1; use draft PRs")

    # ================================================================ main loop
    async def run(self, *, until_idle: bool = False, max_seconds: float | None = None,
                  stop: asyncio.Event | None = None) -> None:
        self.acquire_lock()
        try:
            self.sync_agents()
            if not self._recovered:
                self.recover()
            t0 = time.time()
            idle_ticks = 0
            while not (stop and stop.is_set()):
                busy = await self.tick()
                if max_seconds and time.time() - t0 > max_seconds:
                    break
                if until_idle:
                    idle_ticks = 0 if (busy or self.jobs) else idle_ticks + 1
                    if idle_ticks >= 2:
                        break
                await asyncio.sleep(0.05 if (busy or self.jobs) and until_idle else self.s.poll_interval_s
                                    if not until_idle else 0.1)
            if self.jobs:
                await asyncio.wait(list(self.jobs.values()), timeout=5)
        finally:
            self.release_lock()

    async def tick(self) -> bool:
        if not self._recovered:
            self.recover()
        busy = False
        while self._adoptions:
            self._adopt_attempt(self._adoptions.pop(0))
            busy = True
        self._reap_jobs()
        self._enforce_cancellations()
        busy |= self._start_event_jobs()
        busy |= self._promote_ready()
        busy |= self._dispatch()
        busy |= self._dispatch_reviews()
        busy |= self._resume_stuck()
        await asyncio.sleep(0)
        return busy

    def _spawn_job(self, key: str, coro) -> None:
        async def wrapped():
            try:
                await coro
            except Exception as e:  # never let one job kill the loop
                log.exception("job %s failed", key)
                with tx(self.conn):
                    audit(self.conn, "engine", "job.error", job=key, error=redact(repr(e))[:1500])
        self.jobs[key] = asyncio.get_running_loop().create_task(wrapped())

    def _reap_jobs(self) -> None:
        for k in [k for k, t in self.jobs.items() if t.done()]:
            del self.jobs[k]

    # ================================================================ recovery
    def recover(self) -> list[str]:
        """Reconcile DB state with reality after a crash/reboot.

        - Attempts with exit.json: process finished → collect results normally.
        - Attempts whose wrapper is alive (pid + nonce match): re-adopt.
        - Otherwise: LOST. The task is requeued (bounded), never completed.
        """
        notes: list[str] = []
        self._recovered = True
        for a in rows(self.conn, "SELECT * FROM execution_attempts WHERE status IN ('STARTING','RUNNING')"):
            run_dir = Path(a["run_dir"])
            info = process.read_exit(run_dir, a["nonce"])
            pid = a["pid"] or _find_pid_by_nonce(a["nonce"])
            alive = process.pid_alive_with_nonce(pid, a["nonce"])
            if info or alive:
                notes.append(f"attempt {a['id']}: {'finished' if info else 'still running'} → adopt")
                if pid and not a["pid"]:
                    with tx(self.conn):
                        self.conn.execute("UPDATE execution_attempts SET pid=?, status='RUNNING' WHERE id=?", (pid, a["id"]))
                self._adoptions.append(row(self.conn, "SELECT * FROM execution_attempts WHERE id=?", a["id"]))
                continue
            notes.append(f"attempt {a['id']}: lost")
            self._mark_attempt(a["id"], "LOST", outcome="lost", detail="process lost (crash/reboot)")
            if a["purpose"] == "work" and a["task_id"]:
                self._requeue_or_block(a["task_id"], "execution lost after restart")
            elif a["purpose"] == "tech_lead" and a["event_id"]:
                with tx(self.conn):
                    self.conn.execute("UPDATE events SET status='PENDING' WHERE id=? AND status='PROCESSING'",
                                      (a["event_id"],))
        # events whose processor died without a live attempt
        live_events = {a["event_id"] for a in rows(self.conn, "SELECT event_id FROM execution_attempts WHERE "
                                                              "status IN ('STARTING','RUNNING') AND event_id IS NOT NULL")}
        with tx(self.conn):
            for e in rows(self.conn, "SELECT id FROM events WHERE status='PROCESSING'"):
                if e["id"] not in live_events:
                    self.conn.execute("UPDATE events SET status='PENDING' WHERE id=?", (e["id"],))
                    notes.append(f"event {e['id']}: requeued")
        # DISPATCHED tasks without a live attempt never started
        for t in rows(self.conn, "SELECT id FROM tasks WHERE state='DISPATCHED'"):
            if not row(self.conn, "SELECT 1 FROM execution_attempts WHERE task_id=? AND purpose='work' AND "
                                  "status IN ('STARTING','RUNNING')", t["id"]):
                self._safe_transition(t["id"], S.QUEUED, "recovery: dispatch interrupted", expect=S.DISPATCHED)
                notes.append(f"task {t['id']}: DISPATCHED → QUEUED")
        # RUNNING tasks without any live attempt (e.g. attempt row already LOST)
        for t in rows(self.conn, "SELECT id FROM tasks WHERE state='RUNNING'"):
            if not row(self.conn, "SELECT 1 FROM execution_attempts WHERE task_id=? AND purpose='work' AND "
                                  "status IN ('STARTING','RUNNING','EXITED')", t["id"]):
                self._requeue_or_block(t["id"], "recovery: running task without live process")
                notes.append(f"task {t['id']}: RUNNING → requeued")
        if notes:
            with tx(self.conn):
                audit(self.conn, "engine", "recovery", notes=notes[:100])
        return notes

    def _resume_stuck(self) -> bool:
        """Re-run deterministic steps interrupted by a crash (validation, integration)."""
        busy = False
        for t in rows(self.conn, "SELECT id, state FROM tasks WHERE state IN ('VALIDATING','APPROVED')"):
            key = f"task:{t['id']}"
            if key in self.jobs:
                continue
            if t["state"] == "VALIDATING":
                self._spawn_job(key, self._validate_task(t["id"]))
            else:
                self._spawn_job(key, self._integrate_task(t["id"]))
            busy = True
        return busy

    def _adopt_attempt(self, a: sqlite3.Row) -> None:
        if a["purpose"] == "work":
            self._spawn_job(f"task:{a['task_id']}", self._finish_work_attempt(a["id"]))
        elif a["purpose"] == "review":
            self._spawn_job(f"review:{a['task_id']}", self._finish_review_attempt(a["id"]))
        else:
            ev = row(self.conn, "SELECT * FROM events WHERE id=?", a["event_id"])
            if ev is not None:
                with tx(self.conn):
                    self.conn.execute("UPDATE events SET status='PROCESSING' WHERE id=?", (ev["id"],))
                self._spawn_job(f"tl:{ev['project_id']}", self._finish_tl_attempt(ev, a["id"]))

    def _enforce_cancellations(self) -> None:
        for a in rows(self.conn, "SELECT ea.* FROM execution_attempts ea JOIN tasks t ON t.id=ea.task_id WHERE "
                                 "ea.status='RUNNING' AND ea.purpose='work' AND t.state='CANCELLED'"):
            process.terminate(a["pid"], a["nonce"])

    # ================================================================ execution core
    def _start_attempt(self, agent_id: str, purpose: str, *, task_id: int | None, event_id: int | None,
                       prompt: str, cwd: Path, schema: dict, timeout_s: float, model: str | None = None) -> int:
        ad = self.adapter(agent_id)
        nonce = uuid.uuid4().hex
        run_dir = self.s.artifacts_dir / "runs" / f"{time.strftime('%Y%m%d')}-{nonce[:12]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        req = ExecRequest(purpose=purpose, prompt=prompt, cwd=cwd, run_dir=run_dir, timeout_s=timeout_s,
                          output_schema=schema, model=model)
        inv = ad.prepare(req)
        if inv.stdin_prompt is not None:
            (run_dir / "prompt.txt").write_text(inv.stdin_prompt)
        (run_dir / "prompt.redacted.txt").write_text(redact(prompt))
        (run_dir / "argv.json").write_text(json.dumps([redact(x)[:300] for x in inv.argv]))
        with tx(self.conn):
            aid = self.conn.execute(
                "INSERT INTO execution_attempts (task_id, agent_id, purpose, status, nonce, run_dir, cwd, started_at, "
                "deadline_at, event_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, agent_id, purpose, "STARTING", nonce, str(run_dir), str(cwd), now_iso(),
                 now_iso(timeout_s + 30), event_id)).lastrowid
            quota.record_start(self.conn, agent_id, aid, purpose)
            self._artifact(task_id, aid, "prompt", run_dir / "prompt.redacted.txt")
        pid = process.spawn_detached(inv.argv, run_dir=run_dir, cwd=inv.cwd, nonce=nonce, timeout_s=timeout_s,
                                     env=inv.env)
        with tx(self.conn):
            self.conn.execute("UPDATE execution_attempts SET pid=?, status='RUNNING' WHERE id=?", (pid, aid))
        return aid

    async def _await_attempt(self, attempt_id: int) -> AgentResult:
        a = row(self.conn, "SELECT * FROM execution_attempts WHERE id=?", attempt_id)
        run_dir = Path(a["run_dir"])
        deadline = parse_ts(a["deadline_at"]).timestamp()
        info = await process.wait_exit(a["pid"], run_dir, a["nonce"], max(deadline, time.time() + 1))
        if info is None and process.pid_alive_with_nonce(a["pid"], a["nonce"]):
            process.terminate(a["pid"], a["nonce"])
            info = process.read_exit(run_dir, a["nonce"])
            res = AgentResult("timeout", detail="deadline exceeded; process group terminated")
        else:
            res = self.adapter(a["agent_id"]).collect_result(run_dir, info)
        # redact persisted outputs in place, register artifacts
        for name, kind in (("stdout.log", "stdout"), ("stderr.log", "stderr")):
            f = run_dir / name
            if f.exists():
                f.write_text(redact(f.read_text(errors="replace")))
        status = {"timeout": "TIMED_OUT", "lost": "LOST"}.get(res.outcome, "COLLECTED")
        with tx(self.conn):
            self.conn.execute(
                "UPDATE execution_attempts SET status=?, ended_at=?, exit_code=?, duration_s=?, outcome=?, "
                "failure_detail=?, input_tokens=?, output_tokens=?, cost_usd_reported=? WHERE id=?",
                (status, now_iso(), info.exit_code if info else None, res.duration_s or (info.duration_s if info else None),
                 res.outcome, redact(res.detail)[:2000], res.input_tokens, res.output_tokens, res.cost_usd_reported,
                 attempt_id))
            quota.record_end(self.conn, attempt_id, res.outcome, res.duration_s, res.input_tokens, res.output_tokens,
                             res.retry_after_s)
            for name, kind in (("stdout.log", "stdout"), ("stderr.log", "stderr")):
                if (run_dir / name).exists():
                    self._artifact(a["task_id"], attempt_id, kind, run_dir / name)
        quota.on_outcome(self.conn, a["agent_id"], res.outcome, res.retry_after_s)
        return res

    def _mark_attempt(self, attempt_id: int, status: str, *, outcome: str, detail: str) -> None:
        with tx(self.conn):
            self.conn.execute("UPDATE execution_attempts SET status=?, outcome=?, failure_detail=?, ended_at=? WHERE id=?",
                              (status, outcome, detail, now_iso(), attempt_id))
            self.conn.execute("UPDATE quota_usage SET outcome=? WHERE attempt_id=?", (outcome, attempt_id))

    def _artifact(self, task_id: int | None, attempt_id: int | None, kind: str, path: Path, summary: str = "") -> None:
        self.conn.execute("INSERT INTO artifacts (task_id, attempt_id, kind, path, size_bytes, summary, created_at) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (task_id, attempt_id, kind, str(path), path.stat().st_size if path.exists() else 0,
                           summary[:500], now_iso()))

    def _safe_transition(self, task_id: int, dst: S, reason: str, actor: str = "engine", **kw) -> bool:
        try:
            transition(self.conn, task_id, dst, actor=actor, reason=reason, **kw)
            return True
        except (InvalidTransition, StaleTransition) as e:
            log.info("transition skipped: %s", e)
            return False

    def _requeue_or_block(self, task_id: int, reason: str) -> None:
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if t is None or S(t["state"]) in TERMINAL:
            return
        fails = row(self.conn, "SELECT COUNT(*) c FROM execution_attempts WHERE task_id=? AND purpose='work' AND "
                               "outcome IN ('failed','timeout','lost','malformed','unavailable')", task_id)["c"]
        m = self.manifest(t["project_id"])
        if fails < self._limits(m)["max_execution_failures"]:
            self._safe_transition(task_id, S.QUEUED, f"{reason} (failure {fails}, requeued)")
        else:
            if self._safe_transition(task_id, S.BLOCKED, f"{reason}; failure limit reached", blocked_reason=reason):
                with tx(self.conn):
                    evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=task_id, payload={"reason": f"{reason}; {fails} execution failures"},
                             dedupe_key=f"blocked:{task_id}:{t['state_version']}")

    # ================================================================ events → Tech Lead
    def _start_event_jobs(self) -> bool:
        busy = False
        while True:
            in_flight = {int(k.split(":")[1]) for k in self.jobs if k.startswith("tl:")}
            paused = {r["id"] for r in rows(self.conn, "SELECT id FROM projects WHERE paused=1")}
            ev = evq.claim_next(self.conn, self.worker_id, exclude_projects=in_flight | paused,
                                types=evq.TL_EVENTS | evq.SYSTEM_EVENTS)
            if ev is None:
                return busy
            busy = True
            if ev["type"] in evq.SYSTEM_EVENTS:
                self._spawn_job(f"tl:{ev['project_id']}", self._handle_system_event(ev))
            else:
                self._spawn_job(f"tl:{ev['project_id']}", self._handle_tl_event(ev))

    async def _handle_tl_event(self, ev: sqlite3.Row) -> None:
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", ev["project_id"])
        milestone = row(self.conn, "SELECT * FROM milestones WHERE id=?", ev["milestone_id"])
        m = self.manifest(project["id"])
        lim = self._limits(m)
        if milestone is None or milestone["status"] in ("COMPLETED", "CANCELLED"):
            with tx(self.conn):
                evq.finish(self.conn, ev["id"], "DONE", "milestone not active")
            return
        if ev["type"] == "GOAL_SUBMITTED":
            await asyncio.to_thread(self._ensure_integration_branch_sync, project, milestone, m)
            milestone = row(self.conn, "SELECT * FROM milestones WHERE id=?", ev["milestone_id"])
        if milestone["tl_invocations"] >= lim["max_tl_invocations_per_milestone"]:
            with tx(self.conn):
                evq.finish(self.conn, ev["id"], "FAILED", "tech lead invocation cap reached")
                self.conn.execute("UPDATE milestones SET status='BLOCKED' WHERE id=?", (milestone["id"],))
                self._request_approval(project["id"], "other", "Tech Lead invocation cap reached for milestone "
                                       f"{milestone['key']}; review progress before continuing.", "engine",
                                       milestone_id=milestone["id"], dedupe_key=f"tlcap:{milestone['id']}")
            return
        lead = m.project.tech_lead
        ok, why = quota.admission(self.conn, lead, "tech_lead")
        if not ok:
            with tx(self.conn):
                evq.defer(self.conn, ev["id"], 30, f"tech lead unavailable: {why}")
            return
        prompt = build_tech_lead_prompt(
            self.conn, project=project, milestone=milestone, event=ev,
            manifest={"charter": m.charter, "denied_categories": m.policy.denied_categories(),
                      "validation_names": list(m.validation.commands)},
            agents=self._agent_roster(m), limits={k: lim[k] for k in ("max_task_depth", "max_tasks_per_milestone",
                                                                        "max_revisions")},
            previous_rejection=ev["error"] if ev["attempts"] > 1 else None)
        with tx(self.conn):
            self.conn.execute("UPDATE milestones SET tl_invocations=tl_invocations+1 WHERE id=?", (milestone["id"],))
        lead_cfg = jload(row(self.conn, "SELECT config_json FROM agents WHERE id=?", lead)["config_json"], {})
        model = (lead_cfg.get("event_models") or {}).get(ev["type"])   # e.g. strong model for planning only
        aid = self._start_attempt(lead, "tech_lead", task_id=None, event_id=ev["id"], prompt=prompt,
                                  cwd=self.s.data_dir, schema=envelope_json_schema(),
                                  timeout_s=lim["tech_lead_timeout_s"], model=model)
        await self._finish_tl_attempt(ev, aid)

    async def _finish_tl_attempt(self, ev: sqlite3.Row, attempt_id: int) -> None:
        res = await self._await_attempt(attempt_id)
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", ev["project_id"])
        milestone = row(self.conn, "SELECT * FROM milestones WHERE id=?", ev["milestone_id"])
        m = self.manifest(project["id"])
        lim = self._limits(m)
        ev = row(self.conn, "SELECT * FROM events WHERE id=?", ev["id"])
        if res.outcome in quota.QUOTA_OUTCOMES or res.outcome == "auth":
            with tx(self.conn):
                evq.defer(self.conn, ev["id"], res.retry_after_s or 60, f"tech lead {res.outcome}")
            return
        if res.outcome not in ("success", "malformed"):
            self._tl_failed(ev, project, milestone, f"tech lead invocation {res.outcome}: {res.detail}", lim)
            return
        try:
            if res.outcome == "malformed" and res.structured is None and not res.text.strip():
                raise MalformedDecision(res.detail or "no structured output")
            env = parse_envelope(res.structured if res.structured is not None else res.text)
        except MalformedDecision as e:
            with tx(self.conn):
                self.conn.execute("INSERT INTO decisions (project_id, milestone_id, event_id, attempt_id, decision_type, "
                                  "payload_json, status, rejection_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (project["id"], milestone["id"], ev["id"], attempt_id, None,
                                   redact(jdump(res.structured or res.text[:4000])), "malformed", str(e)[:2000], now_iso()))
            self._tl_failed(ev, project, milestone, f"malformed response: {e}", lim)
            return
        applied, rejected = self.apply_decisions(env, ev, project, milestone, attempt_id)
        if applied == 0:
            self._tl_failed(ev, project, milestone, "all decisions rejected: " + "; ".join(rejected)[:1500], lim)
            return
        with tx(self.conn):
            evq.finish(self.conn, ev["id"], "DONE", "; ".join(rejected)[:2000] or None)

    def _tl_failed(self, ev, project, milestone, reason: str, lim: dict) -> None:
        with tx(self.conn):
            if ev["attempts"] < lim["max_event_attempts"]:
                self.conn.execute("UPDATE events SET status='PENDING', error=? WHERE id=?", (reason[:2000], ev["id"]))
            else:
                evq.finish(self.conn, ev["id"], "FAILED", reason[:2000])
                self._request_approval(project["id"], "other",
                                       f"Tech Lead could not produce a valid decision for event {ev['type']} "
                                       f"after {ev['attempts']} attempts: {reason[:500]}", "engine",
                                       milestone_id=milestone["id"] if milestone else None, task_id=ev["task_id"],
                                       dedupe_key=f"tlfail:{ev['id']}")
            audit(self.conn, "engine", "tech_lead.rejected", project_id=project["id"], reason=reason[:500])

    def _agent_roster(self, m: Manifest) -> dict:
        enabled = {r["id"]: r for r in rows(self.conn, "SELECT id, roles_json, config_json FROM agents WHERE enabled=1")}
        roles = {k: jload(r["roles_json"], []) for k, r in enabled.items()}
        workers = [a for a in dict.fromkeys([m.project.default_worker, *m.project.workers]) if "worker" in roles.get(a, [])]
        reviewers = [a for a in m.project.reviewers if "reviewer" in roles.get(a, [])]
        # owner-written cost/strength notes so the Tech Lead routes work to the right quota pool
        profiles = {a: jload(enabled[a]["config_json"], {}).get("profile", "") for a in dict.fromkeys(workers + reviewers)}
        out: dict = {"workers": workers, "reviewers": reviewers, "default_worker": m.project.default_worker}
        if any(profiles.values()):
            out["profiles"] = {k: v for k, v in profiles.items() if v}
        return out

    # ---------------------------------------------------------------- decision application
    def apply_decisions(self, env: DecisionEnvelope, ev: sqlite3.Row, project: sqlite3.Row,
                        milestone: sqlite3.Row, attempt_id: int | None) -> tuple[int, list[str]]:
        m = self.manifest(project["id"])
        lim = self._limits(m)
        roster = self._agent_roster(m)
        applied, rejected = 0, []
        for d in env.decisions[: lim["max_decisions_per_response"]]:
            try:
                with tx(self.conn):
                    self._apply_one(d, ev, project, milestone, m, lim, roster)
                    self._record_decision(project, milestone, ev, attempt_id, d.type, d.model_dump(), "applied", None)
                applied += 1
            except (PolicyViolation, InvalidTransition, StaleTransition, ValueError) as e:
                reason = f"{d.type}: {e}"
                rejected.append(reason)
                with tx(self.conn):
                    self._record_decision(project, milestone, ev, attempt_id, d.type, d.model_dump(), "rejected",
                                          str(e)[:2000])
                    audit(self.conn, "policy", "decision.rejected", project_id=project["id"], reason=reason[:500])
        return applied, rejected

    def _record_decision(self, project, milestone, ev, attempt_id, dtype, payload, status, reason) -> None:
        self.conn.execute("INSERT INTO decisions (project_id, milestone_id, event_id, attempt_id, decision_type, "
                          "payload_json, status, rejection_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (project["id"], milestone["id"], ev["id"] if ev else None, attempt_id, dtype,
                           redact(jdump(payload)), status, reason, now_iso()))

    def _task_in_milestone(self, task_id: int, milestone: sqlite3.Row) -> sqlite3.Row:
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if t is None or t["milestone_id"] != milestone["id"]:
            raise PolicyViolation(f"task {task_id} is not part of milestone {milestone['key']}")
        return t

    def _apply_one(self, d, ev, project, milestone, m: Manifest, lim: dict, roster: dict) -> None:
        denied = m.policy.denied_categories()
        if d.type == "CREATE_TASKS":
            existing = row(self.conn, "SELECT COUNT(*) c FROM tasks WHERE milestone_id=?", milestone["id"])["c"]
            if existing + len(d.tasks) > lim["max_tasks_per_milestone"]:
                raise PolicyViolation(f"task cap: {existing}+{len(d.tasks)} > {lim['max_tasks_per_milestone']}")
            keys = {r["key"]: r["id"] for r in rows(self.conn, "SELECT key, id FROM tasks WHERE milestone_id=?",
                                                    milestone["id"])}
            batch = {nt.key for nt in d.tasks}
            for nt in d.tasks:
                if nt.key in keys:
                    raise PolicyViolation(f"task key {nt.key!r} already exists")
                if nt.assignee not in roster["workers"]:
                    raise PolicyViolation(f"assignee {nt.assignee!r} is not an enabled worker for this project")
                hits = hardware_no_go_hits(f"{nt.title}\n{nt.description}\n{' '.join(nt.acceptance_criteria)}", denied)
                if hits:
                    raise PolicyViolation(f"task {nt.key!r} touches denied categories {hits}")
                for v in nt.validation or []:
                    if v not in m.validation.commands:
                        raise PolicyViolation(f"unknown validation command {v!r}")
                for dep in nt.depends_on:
                    if dep not in keys and dep not in batch:
                        raise PolicyViolation(f"unknown dependency {dep!r}")
                depth = 0
                if nt.parent_task_id is not None:
                    parent = self._task_in_milestone(nt.parent_task_id, milestone)
                    depth = parent["depth"] + 1
                if depth > lim["max_task_depth"]:
                    raise PolicyViolation(f"task depth {depth} exceeds {lim['max_task_depth']}")
            _check_acyclic({nt.key: nt.depends_on for nt in d.tasks})
            for nt in d.tasks:
                depth = 0 if nt.parent_task_id is None else row(
                    self.conn, "SELECT depth FROM tasks WHERE id=?", nt.parent_task_id)["depth"] + 1
                tid = self.conn.execute(
                    "INSERT INTO tasks (project_id, milestone_id, parent_task_id, key, title, description, kind, "
                    "acceptance_json, validation_json, allowed_paths_json, assignee, depth, timeout_seconds, created_at, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (project["id"], milestone["id"], nt.parent_task_id, nt.key, nt.title, nt.description, nt.kind,
                     jdump(nt.acceptance_criteria), jdump(_validation_for(nt, m)),
                     jdump(nt.allowed_paths), nt.assignee, depth, nt.timeout_s or lim["default_task_timeout_s"],
                     now_iso(), now_iso())).lastrowid
                keys[nt.key] = tid
                self.conn.execute("INSERT INTO task_transitions (task_id, from_state, to_state, reason, actor, created_at)"
                                  " VALUES (?,?,?,?,?,?)", (tid, "-", "CREATED", "created by tech lead", "tech_lead",
                                                            now_iso()))
            for nt in d.tasks:
                for dep in nt.depends_on:
                    self.conn.execute("INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (?,?)",
                                      (keys[nt.key], keys[dep]))
            return
        if d.type == "ASSIGN_TASK":
            t = self._task_in_milestone(d.task_id, milestone)
            if d.assignee not in roster["workers"]:
                raise PolicyViolation(f"assignee {d.assignee!r} is not an enabled worker")
            if t["state"] not in ("CREATED", "QUEUED", "BLOCKED"):
                raise InvalidTransition(f"cannot reassign task in state {t['state']}")
            self.conn.execute("UPDATE tasks SET assignee=?, updated_at=? WHERE id=?", (d.assignee, now_iso(), t["id"]))
            if t["state"] == "BLOCKED":
                transition(self.conn, t["id"], S.QUEUED, actor="tech_lead", reason=f"reassigned to {d.assignee}",
                           blocked_reason=None, validation_retries=0)
            return
        if d.type == "REQUEST_REVISION":
            t = self._task_in_milestone(d.task_id, milestone)
            if t["revision_count"] >= lim["max_revisions"]:
                raise PolicyViolation(f"revision limit {lim['max_revisions']} reached for task {t['key']}")
            if t["state"] in ("REVIEW_PENDING", "APPROVED"):
                transition(self.conn, t["id"], S.REVISION_REQUIRED, actor="tech_lead", reason="revision requested",
                           feedback=d.feedback, reviewer=None)
                transition(self.conn, t["id"], S.QUEUED, actor="tech_lead", reason="revision queued",
                           revision_count=t["revision_count"] + 1, validation_retries=0)
            elif t["state"] == "BLOCKED":
                transition(self.conn, t["id"], S.QUEUED, actor="tech_lead", reason="revision requested (unblock)",
                           feedback=d.feedback, revision_count=t["revision_count"] + 1, validation_retries=0,
                           blocked_reason=None, reviewer=None)
            else:
                raise InvalidTransition(f"cannot request revision in state {t['state']}")
            return
        if d.type == "REQUEST_REVIEW":
            t = self._task_in_milestone(d.task_id, milestone)
            if t["state"] != "REVIEW_PENDING":
                raise InvalidTransition(f"review requires REVIEW_PENDING, task is {t['state']}")
            if d.reviewer not in roster["reviewers"]:
                raise PolicyViolation(f"{d.reviewer!r} is not an enabled reviewer for this project")
            if d.reviewer == t["assignee"]:
                raise PolicyViolation("reviewer must differ from the task's author")
            if t["reviewer"]:
                raise PolicyViolation("a review is already pending")
            self.conn.execute("UPDATE tasks SET reviewer=?, updated_at=? WHERE id=?", (d.reviewer, now_iso(), t["id"]))
            audit(self.conn, "tech_lead", "review.request", project_id=project["id"], task_id=t["id"],
                  reviewer=d.reviewer, focus=d.focus[:300])
            return
        if d.type == "APPROVE_TASK":
            t = self._task_in_milestone(d.task_id, milestone)
            if t["state"] != "REVIEW_PENDING":
                raise InvalidTransition(f"approve requires REVIEW_PENDING, task is {t['state']}")
            if t["reviewer"]:
                raise PolicyViolation("cannot approve while an independent review is pending")
            vr = row(self.conn, "SELECT * FROM validation_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", t["id"])
            if vr is None or not vr["passed"]:
                raise PolicyViolation("latest deterministic validation did not pass")
            last_review = row(self.conn, "SELECT * FROM reviews WHERE task_id=? AND source IN ('agent','github') "
                                         "ORDER BY id DESC LIMIT 1", t["id"])
            if last_review and last_review["verdict"] == "changes_requested" and \
                    last_review["created_at"] >= vr["created_at"]:
                raise PolicyViolation("latest review requested changes")
            needs_review = m.project.review_policy == "always" or (
                m.project.review_policy == "risk" and m.project.review_risk_paths and any(
                    _match(f["path"], m.project.review_risk_paths) for f in jload(vr["changed_files_json"], [])))
            if needs_review and not (last_review and last_review["verdict"] == "approve"
                                     and last_review["created_at"] >= vr["created_at"]):
                raise PolicyViolation("project review policy requires an approving independent review first")
            self.conn.execute("INSERT INTO reviews (task_id, reviewer, source, verdict, body, created_at) VALUES "
                              "(?,?,?,?,?,?)", (t["id"], m.project.tech_lead, "tech_lead", "approve",
                                                d.note or "approved", now_iso()))
            transition(self.conn, t["id"], S.APPROVED, actor="tech_lead", reason="approved by tech lead")
            return
        if d.type == "BLOCK_TASK":
            t = self._task_in_milestone(d.task_id, milestone)
            if S(t["state"]) in TERMINAL or t["state"] in ("RUNNING", "DISPATCHED", "VALIDATING"):
                raise InvalidTransition(f"cannot block task in state {t['state']}")
            transition(self.conn, t["id"], S.BLOCKED, actor="tech_lead", reason=d.reason, blocked_reason=d.reason)
            return
        if d.type == "COMPLETE_MILESTONE":
            if d.milestone_id != milestone["id"]:
                raise PolicyViolation("can only complete the milestone of the triggering event")
            open_tasks = rows(self.conn, "SELECT key, state FROM tasks WHERE milestone_id=? AND state NOT IN "
                                         "('COMPLETED','CANCELLED')", milestone["id"])
            if open_tasks:
                raise PolicyViolation(f"milestone has unfinished tasks: {[t['key'] for t in open_tasks]}")
            if not row(self.conn, "SELECT 1 FROM tasks WHERE milestone_id=? AND state='COMPLETED'", milestone["id"]):
                raise PolicyViolation("milestone has no completed tasks")
            self.conn.execute("UPDATE milestones SET status='COMPLETED', summary=?, completed_at=? WHERE id=?",
                              (d.summary, now_iso(), milestone["id"]))
            self._add_context(project["id"], "verified_fact", f"Milestone {milestone['key']} completed: {d.summary[:300]}")
            audit(self.conn, "tech_lead", "milestone.complete", project_id=project["id"], milestone=milestone["key"])
            if m.policy.draft_pr != "never":
                self._request_approval(project["id"], "create_draft_pr",
                                       f"Open a draft PR for {milestone['integration_branch']} → {project['base_branch']}",
                                       "engine", milestone_id=milestone["id"],
                                       evidence={"summary": d.summary[:1000], "branch": milestone["integration_branch"]},
                                       dedupe_key=f"pr:{milestone['id']}")
                if m.policy.draft_pr == "auto":
                    a = row(self.conn, "SELECT id FROM approval_requests WHERE dedupe_key=?", f"pr:{milestone['id']}")
                    self.conn.execute("UPDATE approval_requests SET status='APPROVED', resolved_by='policy:auto', "
                                      "resolved_at=? WHERE id=?", (now_iso(), a["id"]))
                    evq.emit(self.conn, "APPROVAL_RESOLVED", project_id=project["id"], milestone_id=milestone["id"],
                             payload={"approval_id": a["id"], "approved": True, "action": "create_draft_pr"},
                             dedupe_key=f"approval:{a['id']}")
            return
        if d.type == "REQUEST_HUMAN_APPROVAL":
            task_id = None
            if d.task_id is not None:
                task_id = self._task_in_milestone(d.task_id, milestone)["id"]
            action = d.action if d.action != "start_milestone" else "other"
            self._request_approval(project["id"], action, d.reason, "tech_lead", milestone_id=milestone["id"],
                                   task_id=task_id, dedupe_key=f"tl:{ev['id']}:{action}:{task_id}" if ev else None)
            return
        raise PolicyViolation(f"unsupported decision {d.type}")

    # ---------------------------------------------------------------- system events
    async def _handle_system_event(self, ev: sqlite3.Row) -> None:
        p = jload(ev["payload_json"], {})
        action, approved = p.get("action"), bool(p.get("approved"))
        try:
            if action == "start_milestone":
                with tx(self.conn):
                    if approved:
                        self.conn.execute("UPDATE milestones SET status='ACTIVE' WHERE id=? AND status='AWAITING_APPROVAL'",
                                          (ev["milestone_id"],))
                        ms = row(self.conn, "SELECT goal FROM milestones WHERE id=?", ev["milestone_id"])
                        evq.emit(self.conn, "GOAL_SUBMITTED", project_id=ev["project_id"], milestone_id=ev["milestone_id"],
                                 payload={"goal": ms["goal"]}, dedupe_key=f"goal:{ev['milestone_id']}")
                    else:
                        self.conn.execute("UPDATE milestones SET status='CANCELLED' WHERE id=?", (ev["milestone_id"],))
            elif action == "create_draft_pr":
                if approved:
                    await self._create_draft_pr(ev["milestone_id"])
            else:
                ms = row(self.conn, "SELECT status FROM milestones WHERE id=?", ev["milestone_id"]) if ev["milestone_id"] else None
                if ms and ms["status"] == "ACTIVE":
                    with tx(self.conn):
                        evq.emit(self.conn, "HUMAN_DECISION", project_id=ev["project_id"], milestone_id=ev["milestone_id"],
                                 task_id=ev["task_id"], payload=p, dedupe_key=f"human:{p.get('approval_id')}")
            with tx(self.conn):
                evq.finish(self.conn, ev["id"], "DONE")
        except Exception as e:
            with tx(self.conn):
                evq.finish(self.conn, ev["id"], "FAILED", redact(str(e))[:2000])
                audit(self.conn, "engine", "system_event.failed", project_id=ev["project_id"], error=redact(str(e))[:500])

    async def _create_draft_pr(self, milestone_id: int) -> None:
        ms = row(self.conn, "SELECT * FROM milestones WHERE id=?", milestone_id)
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", ms["project_id"])
        m = self.manifest(project["id"])
        ws = self.workspace(project, m)
        integ = self.s.workspaces_dir / project["slug"] / f"{slugify(ms['key'])}-integration"
        vres = await asyncio.to_thread(run_validation, integ, m.validation.default, m.validation.commands,
                                       self.s.validation_executables, self.s.artifacts_dir / "milestones" / str(ms["id"]),
                                       m.validation.timeout_s)
        if not vres.passed:
            raise RuntimeError(f"integration branch validation failed: {vres.summary()}")
        repo = repo_slug_from_url(project["remote_url"])
        if not repo:
            raise RuntimeError("project has no GitHub remote_url")
        await asyncio.to_thread(ws.push_branch, ms["integration_branch"])
        body = f"## {ms['key']}\n\n{ms['goal']}\n\n### Summary\n{ms['summary'] or ''}\n\n### Tasks\n" + "\n".join(
            f"- {t['key']}: {t['title']} ({t['state']})" for t in rows(self.conn, "SELECT key, title, state FROM tasks "
                                                                              "WHERE milestone_id=?", ms["id"]))
        body += f"\n\nValidation: {vres.summary()}\n\n_Opened as a draft by AgentBridge. Not merged automatically._"
        url, num = await asyncio.to_thread(self.github.create_draft_pr, repo=repo, base=project["base_branch"],
                                           head=ms["integration_branch"], title=f"[AgentBridge] {ms['key']}: {ms['goal'][:80]}",
                                           body=body, workdir=self.s.artifacts_dir, protected=self._protected(m))
        with tx(self.conn):
            self.conn.execute("UPDATE milestones SET pr_url=?, pr_number=? WHERE id=?", (url, num, ms["id"]))
            self._add_context(project["id"], "outstanding_pr", f"{ms['key']}: {url}")
            audit(self.conn, "engine", "github.draft_pr", project_id=project["id"], url=url)

    # ================================================================ scheduling
    def _promote_ready(self) -> bool:
        busy = False
        for t in rows(self.conn, "SELECT t.* FROM tasks t JOIN milestones ms ON ms.id=t.milestone_id "
                                 "WHERE t.state='CREATED' AND ms.status='ACTIVE'"):
            unmet = row(self.conn, "SELECT COUNT(*) c FROM task_dependencies d JOIN tasks x ON x.id=d.depends_on_id "
                                   "WHERE d.task_id=? AND x.state != 'COMPLETED'", t["id"])["c"]
            if unmet == 0:
                busy |= self._safe_transition(t["id"], S.QUEUED, "dependencies satisfied", expect=S.CREATED)
        return busy

    def _dispatch(self) -> bool:
        busy = False
        global_running = row(self.conn, "SELECT COUNT(*) c FROM execution_attempts WHERE purpose!='tech_lead' AND "
                                        "status IN ('STARTING','RUNNING')")["c"]
        for t in rows(self.conn, "SELECT t.* FROM tasks t JOIN milestones ms ON ms.id=t.milestone_id JOIN projects p "
                                 "ON p.id=t.project_id WHERE t.state='QUEUED' AND ms.status='ACTIVE' AND p.paused=0 "
                                 "ORDER BY t.id"):
            if global_running >= self.s.limits.max_concurrent_agents:
                break
            if f"task:{t['id']}" in self.jobs:
                continue
            ok, why = quota.admission(self.conn, t["assignee"], "work")
            if not ok:
                continue  # this agent is unavailable; other agents' tasks still dispatch
            problem = self._config_problem(t["assignee"])
            if problem:
                if self._safe_transition(t["id"], S.BLOCKED, problem, blocked_reason=problem):
                    with tx(self.conn):
                        evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                                 task_id=t["id"], payload={"reason": f"{t['assignee']}: {problem}; reassign"},
                                 dedupe_key=f"blocked:{t['id']}:config:{t['state_version']}")
                continue
            m = self.manifest(t["project_id"])
            hits = hardware_no_go_hits(f"{t['title']}\n{t['description']}", m.policy.denied_categories())
            if hits:
                self._safe_transition(t["id"], S.BLOCKED, f"hardware no-go: {hits}", blocked_reason="hardware no-go")
                continue
            # transaction-safe claim: CAS on state/version
            token = uuid.uuid4().hex
            try:
                transition(self.conn, t["id"], S.DISPATCHED, actor="engine", reason=f"dispatch to {t['assignee']}",
                           expect=S.QUEUED, claim_token=token)
            except (StaleTransition, InvalidTransition):
                continue
            global_running += 1
            busy = True
            self._spawn_job(f"task:{t['id']}", self._run_task(t["id"], token))
        return busy

    def _dispatch_reviews(self) -> bool:
        busy = False
        for t in rows(self.conn, "SELECT * FROM tasks WHERE state='REVIEW_PENDING' AND reviewer IS NOT NULL"):
            key = f"review:{t['id']}"
            if key in self.jobs:
                continue
            if row(self.conn, "SELECT 1 FROM execution_attempts WHERE task_id=? AND purpose='review' AND "
                              "status IN ('STARTING','RUNNING')", t["id"]):
                continue
            ok, _ = quota.admission(self.conn, t["reviewer"], "review")
            if not ok:
                continue
            problem = self._config_problem(t["reviewer"])
            if problem:
                with tx(self.conn):
                    self.conn.execute("UPDATE tasks SET reviewer=NULL WHERE id=?", (t["id"],))
                    evq.emit(self.conn, "REVIEW_COMPLETED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=t["id"], payload={"verdict": "unavailable", "reviewer": t["reviewer"],
                                                       "issues": [problem]},
                             dedupe_key=f"review:config:{t['id']}:{t['state_version']}:{t['reviewer']}")
                continue
            busy = True
            self._spawn_job(key, self._run_review(t["id"]))
        return busy

    # ---------------------------------------------------------------- task execution
    def _ensure_integration_branch_sync(self, project, milestone, m: Manifest) -> None:
        if milestone["integration_branch"]:
            return
        ws = self.workspace(project, m)
        try:
            ws.fetch()
        except GitError as e:
            log.warning("fetch failed: %s", e)
        base = ws.approved_base(project["base_branch"])
        branch = f"agentbridge/{project['slug']}/{slugify(milestone['key'])}/integration"
        path = self.s.workspaces_dir / project["slug"] / f"{slugify(milestone['key'])}-integration"
        ws.create_worktree(path, branch, base)
        with tx(self.conn):
            self.conn.execute("UPDATE milestones SET integration_branch=?, base_ref=? WHERE id=?",
                              (branch, base, milestone["id"]))
            self.conn.execute("INSERT INTO git_refs (project_id, milestone_id, ref_name, sha, kind, created_at) VALUES "
                              "(?,?,?,?,?,?)", (project["id"], milestone["id"], branch, base, "base", now_iso()))

    def _prepare_task_worktree_sync(self, t, project, milestone, m: Manifest) -> tuple[Path, str, str]:
        ws = self.workspace(project, m)
        branch = t["branch"] or f"agentbridge/{project['slug']}/{slugify(milestone['key'])}/{slugify(t['key'])}"
        path = Path(t["worktree_path"]) if t["worktree_path"] else \
            self.s.workspaces_dir / project["slug"] / f"{slugify(milestone['key'])}-{slugify(t['key'])}"
        base = t["base_sha"] or ws.resolve(f"refs/heads/{milestone['integration_branch']}")
        ws.create_worktree(path, branch, base)
        return path.resolve(), branch, base

    async def _run_task(self, task_id: int, claim_token: str) -> None:
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if t["claim_token"] != claim_token:
            return
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", t["project_id"])
        milestone = row(self.conn, "SELECT * FROM milestones WHERE id=?", t["milestone_id"])
        m = self.manifest(project["id"])
        try:
            path, branch, base = await asyncio.to_thread(self._prepare_task_worktree_sync, t, project, milestone, m)
            with tx(self.conn):
                lease = row(self.conn, "SELECT * FROM worktree_leases WHERE path=?", str(path))
                if lease and lease["task_id"] != task_id:
                    raise PolicyViolation(f"worktree {path} is leased by task {lease['task_id']}")
                self.conn.execute("INSERT OR REPLACE INTO worktree_leases (path, project_id, task_id, agent_id, "
                                  "leased_at) VALUES (?,?,?,?,?)",
                                  (str(path), project["id"], task_id, t["assignee"], now_iso()))
                self.conn.execute("UPDATE tasks SET branch=?, worktree_path=?, base_sha=? WHERE id=?",
                                  (branch, str(path), base, task_id))
        except (GitError, PolicyViolation) as e:
            reason = f"worktree setup failed: {redact(str(e))[:500]}"
            if self._safe_transition(task_id, S.QUEUED, reason, expect=S.DISPATCHED) and \
                    self._safe_transition(task_id, S.BLOCKED, reason, blocked_reason=reason):
                with tx(self.conn):
                    evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=task_id, payload={"reason": reason},
                             dedupe_key=f"blocked:{task_id}:setup:{t['state_version']}")
            return
        transition(self.conn, task_id, S.RUNNING, actor="engine", reason="worker started", expect=S.DISPATCHED,
                   attempt_count=t["attempt_count"] + 1)
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        prompt = build_worker_prompt(t, project=project, manifest={"denied_categories": m.policy.denied_categories()},
                                     worktree=str(path))
        try:
            aid = self._start_attempt(t["assignee"], "work", task_id=task_id, event_id=None, prompt=prompt, cwd=path,
                                      schema=worker_result_schema(), timeout_s=t["timeout_seconds"])
        except Exception as e:
            self._requeue_or_block(task_id, f"failed to start worker: {e}")
            return
        await self._finish_work_attempt(aid)

    async def _finish_work_attempt(self, attempt_id: int) -> None:
        a = row(self.conn, "SELECT * FROM execution_attempts WHERE id=?", attempt_id)
        res = await self._await_attempt(attempt_id)
        task_id = a["task_id"]
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if t["state"] != "RUNNING":
            return  # cancelled or otherwise moved while running
        if res.outcome in quota.QUOTA_OUTCOMES:
            m = self.manifest(t["project_id"])
            if t["quota_requeues"] + 1 > self._limits(m)["max_quota_requeues"]:
                if self._safe_transition(task_id, S.BLOCKED, f"repeated {res.outcome}", blocked_reason=res.outcome):
                    with tx(self.conn):
                        evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                                 task_id=task_id, payload={"reason": f"agent {t['assignee']} repeatedly {res.outcome}"},
                                 dedupe_key=f"blocked:{task_id}:quota:{t['quota_requeues']}")
            else:
                self._safe_transition(task_id, S.QUEUED, f"{res.outcome}; agent cooling down",
                                      quota_requeues=t["quota_requeues"] + 1)
            return
        if res.outcome == "auth":
            if self._safe_transition(task_id, S.BLOCKED, "agent authentication failed", blocked_reason="agent auth"):
                with tx(self.conn):
                    evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=task_id, payload={"reason": f"agent {t['assignee']} is not authenticated; reassign "
                                                                 "or wait for owner re-login"},
                             dedupe_key=f"blocked:{task_id}:auth:{attempt_id}")
            return
        if res.outcome != "success":
            self._requeue_or_block(task_id, f"worker {res.outcome}: {res.detail[:300]}")
            return
        try:
            wr = WorkerResult.model_validate(res.structured or {})
        except Exception as e:
            self._mark_outcome(attempt_id, "malformed", f"worker result invalid: {e}")
            self._requeue_or_block(task_id, "worker returned an invalid result")
            return
        summary = redact(wr.summary)
        if wr.status == "completed":
            transition(self.conn, task_id, S.VALIDATING, actor="engine", reason="worker completed",
                       worker_summary=summary)
            await self._validate_task(task_id, attempt_id)
        else:
            # keep partial work: commit it so nothing is lost
            try:
                await asyncio.to_thread(self._commit_sync, t, f"agentbridge: {t['key']} partial ({wr.status})")
            except GitError:
                pass
            if self._safe_transition(task_id, S.BLOCKED, f"worker reported {wr.status}", worker_summary=summary,
                                     blocked_reason=f"worker {wr.status}: {summary[:300]}"):
                with tx(self.conn):
                    evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=task_id, payload={"reason": f"worker {wr.status}: {summary[:500]}"},
                             dedupe_key=f"blocked:{task_id}:worker:{attempt_id}")

    def _mark_outcome(self, attempt_id: int, outcome: str, detail: str) -> None:
        with tx(self.conn):
            self.conn.execute("UPDATE execution_attempts SET outcome=?, failure_detail=? WHERE id=?",
                              (outcome, redact(detail)[:2000], attempt_id))

    def _commit_sync(self, t, message: str) -> str | None:
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", t["project_id"])
        ws = self.workspace(project)
        return ws.commit_all(Path(t["worktree_path"]), message)

    async def _validate_task(self, task_id: int, attempt_id: int | None = None) -> None:
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if t["state"] != "VALIDATING":
            return
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", t["project_id"])
        m = self.manifest(project["id"])
        lim = self._limits(m)
        ws = self.workspace(project, m)
        path = Path(t["worktree_path"])
        out_dir = self.s.artifacts_dir / "tasks" / str(task_id) / f"attempt-{t['attempt_count']}"

        def work():
            ws.commit_all(path, f"agentbridge: {t['key']} attempt {t['attempt_count']}\n\nTask-Id: {task_id}")
            cs = ws.changes(path, t["base_sha"])
            problems = ws.unexpected_changes(path, cs, forbidden=m.policy.forbidden_paths,
                                             allowed=jload(t["allowed_paths_json"], []),
                                             max_files=m.policy.max_changed_files)
            names = jload(t["validation_json"], [])
            vres = run_validation(path, names, m.validation.commands, self.s.validation_executables, out_dir,
                                  m.validation.timeout_s)
            return cs, problems, vres

        try:
            cs, problems, vres = await asyncio.to_thread(work)
        except (GitError, PolicyViolation) as e:
            if self._safe_transition(task_id, S.BLOCKED, f"validation setup failed: {e}", blocked_reason=str(e)[:500]):
                with tx(self.conn):
                    evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=task_id, payload={"reason": f"validation error: {redact(str(e))[:500]}"},
                             dedupe_key=f"blocked:{task_id}:valerr:{t['state_version']}")
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        diff_file = out_dir / "diff.patch"
        diff_file.write_text(redact(cs.diff))
        if t["kind"] == "code" and not cs.files:
            problems.append("code task produced no changes")
        with tx(self.conn):
            self._artifact(task_id, attempt_id, "diff", diff_file, f"{len(cs.files)} files")
            for r in vres.results:
                self._artifact(task_id, attempt_id, "validation_log", Path(r["log"]), r["name"])
            self.conn.execute("INSERT INTO validation_runs (task_id, attempt_id, passed, results_json, "
                              "policy_violations_json, changed_files_json, created_at) VALUES (?,?,?,?,?,?,?)",
                              (task_id, attempt_id, int(vres.passed and not problems), jdump(vres.results),
                               jdump(vres.violations + problems), jdump(cs.files), now_iso()))
            self.conn.execute("UPDATE tasks SET head_sha=? WHERE id=?", (cs.head_sha, task_id))
            self.conn.execute("INSERT INTO git_refs (project_id, task_id, ref_name, sha, kind, created_at) VALUES "
                              "(?,?,?,?,?,?)", (project["id"], task_id, t["branch"], cs.head_sha, "task_head", now_iso()))
        if problems or vres.violations:
            reason = "; ".join(vres.violations + problems)
            if self._safe_transition(task_id, S.BLOCKED, f"policy: {reason}", blocked_reason=reason[:1000]):
                with tx(self.conn):
                    evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=task_id, payload={"reason": f"policy violations: {reason[:800]}"},
                             dedupe_key=f"blocked:{task_id}:policy:{cs.head_sha}")
            return
        if not vres.passed:
            fb = "Deterministic validation failed:\n" + "\n".join(
                f"[{r['name']}] exit={r['exit_code']}\n{r['tail']}" for r in vres.results if r["exit_code"] != 0)
            if t["validation_retries"] < lim["max_validation_retries"]:
                transition(self.conn, task_id, S.REVISION_REQUIRED, actor="validation", reason=vres.summary(),
                           feedback=fb[:6000])
                transition(self.conn, task_id, S.QUEUED, actor="validation", reason="automatic retry with feedback",
                           validation_retries=t["validation_retries"] + 1)
            elif self._safe_transition(task_id, S.BLOCKED, "validation failed beyond retry policy",
                                       blocked_reason="validation exhausted", feedback=fb[:6000]):
                with tx(self.conn):
                    evq.emit(self.conn, "VALIDATION_EXHAUSTED", project_id=t["project_id"],
                             milestone_id=t["milestone_id"], task_id=task_id,
                             payload={"reason": vres.summary()}, dedupe_key=f"valx:{task_id}:{cs.head_sha}")
            return
        transition(self.conn, task_id, S.REVIEW_PENDING, actor="validation", reason=vres.summary())
        with tx(self.conn):
            evq.emit(self.conn, "TASK_READY_FOR_REVIEW", project_id=t["project_id"], milestone_id=t["milestone_id"],
                     task_id=task_id, payload={"validation": vres.summary(), "head": cs.head_sha},
                     dedupe_key=f"ready:{task_id}:{cs.head_sha}:{t['revision_count']}")

    # ---------------------------------------------------------------- review
    async def _run_review(self, task_id: int) -> None:
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", t["project_id"])
        m = self.manifest(project["id"])
        ws = self.workspace(project, m)
        n = row(self.conn, "SELECT COUNT(*) c FROM execution_attempts WHERE task_id=? AND purpose='review'", task_id)["c"]
        path = self.s.workspaces_dir / project["slug"] / f"review-{task_id}-{n + 1}-{uuid.uuid4().hex[:6]}"
        await asyncio.to_thread(ws.create_detached_worktree, path, t["head_sha"])
        diff = ""
        art = row(self.conn, "SELECT path FROM artifacts WHERE task_id=? AND kind='diff' ORDER BY id DESC LIMIT 1", task_id)
        if art and Path(art["path"]).exists():
            diff = Path(art["path"]).read_text(errors="replace")
        vr = row(self.conn, "SELECT results_json FROM validation_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", task_id)
        vsum = "; ".join(f"{r['name']}={'PASS' if r['exit_code'] == 0 else 'FAIL'}" for r in jload(vr["results_json"], [])) if vr else "n/a"
        prompt = build_review_prompt(t, diff=diff, validation_summary=vsum)
        aid = self._start_attempt(t["reviewer"], "review", task_id=task_id, event_id=None, prompt=prompt, cwd=path,
                                  schema=review_result_schema(), timeout_s=self.s.limits.tech_lead_timeout_s)
        await self._finish_review_attempt(aid, path)

    async def _finish_review_attempt(self, attempt_id: int, path: Path | None = None) -> None:
        a = row(self.conn, "SELECT * FROM execution_attempts WHERE id=?", attempt_id)
        path = path or Path(a["cwd"])
        res = await self._await_attempt(attempt_id)
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", a["task_id"])
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", t["project_id"])
        ws = self.workspace(project)
        try:
            await asyncio.to_thread(ws.remove_worktree, path)  # refuses if the reviewer modified files
        except GitError as e:
            with tx(self.conn):
                audit(self.conn, "policy", "review.modified_worktree", task_id=t["id"], detail=str(e)[:300])
        if t["state"] != "REVIEW_PENDING" or t["reviewer"] != a["agent_id"]:
            return
        if res.outcome != "success":
            fails = row(self.conn, "SELECT COUNT(*) c FROM execution_attempts WHERE task_id=? AND purpose='review' AND "
                                   "agent_id=? AND outcome != 'success'", t["id"], a["agent_id"])["c"]
            if res.outcome in quota.QUOTA_OUTCOMES and fails < 3:
                return  # retried when the reviewer's cooldown ends; other agents unaffected
            if fails >= 3 or res.outcome not in quota.QUOTA_OUTCOMES:
                with tx(self.conn):
                    self.conn.execute("UPDATE tasks SET reviewer=NULL WHERE id=?", (t["id"],))
                    evq.emit(self.conn, "REVIEW_COMPLETED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=t["id"], payload={"verdict": "unavailable", "reviewer": a["agent_id"],
                                                       "issues": [f"review failed: {res.outcome}"]},
                             dedupe_key=f"review:{attempt_id}")
            return
        try:
            rr = ReviewResult.model_validate(res.structured or {})
        except Exception as e:
            self._mark_outcome(attempt_id, "malformed", str(e))
            with tx(self.conn):
                self.conn.execute("UPDATE tasks SET reviewer=NULL WHERE id=?", (t["id"],))
                evq.emit(self.conn, "REVIEW_COMPLETED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                         task_id=t["id"], payload={"verdict": "unavailable", "reviewer": a["agent_id"],
                                                   "issues": ["reviewer returned malformed output"]},
                         dedupe_key=f"review:{attempt_id}")
            return
        body = redact(rr.summary + ("\n- " + "\n- ".join(rr.issues) if rr.issues else ""))
        with tx(self.conn):
            self.conn.execute("INSERT INTO reviews (task_id, reviewer, source, verdict, body, created_at) VALUES "
                              "(?,?,?,?,?,?)", (t["id"], a["agent_id"], "agent", rr.verdict, body, now_iso()))
            self.conn.execute("UPDATE tasks SET reviewer=NULL WHERE id=?", (t["id"],))
            evq.emit(self.conn, "REVIEW_COMPLETED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                     task_id=t["id"], payload={"verdict": rr.verdict, "reviewer": a["agent_id"],
                                               "issues": [redact(i) for i in rr.issues]},
                     dedupe_key=f"review:{attempt_id}")

    # ---------------------------------------------------------------- integration
    async def _integrate_task(self, task_id: int) -> None:
        t = row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if t["state"] != "APPROVED":
            return
        project = row(self.conn, "SELECT * FROM projects WHERE id=?", t["project_id"])
        milestone = row(self.conn, "SELECT * FROM milestones WHERE id=?", t["milestone_id"])
        m = self.manifest(project["id"])
        ws = self.workspace(project, m)
        integ = self.s.workspaces_dir / project["slug"] / f"{slugify(milestone['key'])}-integration"
        try:
            sha = await asyncio.to_thread(ws.merge_into, integ, milestone["integration_branch"], t["branch"],
                                          f"agentbridge: integrate {t['key']} ({t['title'][:60]})\n\nTask-Id: {task_id}")
        except (GitError, PolicyViolation) as e:
            if self._safe_transition(task_id, S.BLOCKED, f"integration failed: {e}", blocked_reason="merge conflict"):
                with tx(self.conn):
                    evq.emit(self.conn, "TASK_BLOCKED", project_id=t["project_id"], milestone_id=t["milestone_id"],
                             task_id=task_id, payload={"reason": f"integration failed: {redact(str(e))[:500]}"},
                             dedupe_key=f"blocked:{task_id}:merge:{t['head_sha']}")
            return
        transition(self.conn, task_id, S.COMPLETED, actor="engine", reason=f"integrated at {sha[:12]}")
        with tx(self.conn):
            self.conn.execute("INSERT INTO git_refs (project_id, milestone_id, task_id, ref_name, sha, kind, created_at) "
                              "VALUES (?,?,?,?,?,?,?)", (project["id"], milestone["id"], task_id,
                                                         milestone["integration_branch"], sha, "integration", now_iso()))
            self.conn.execute("DELETE FROM worktree_leases WHERE task_id=?", (task_id,))
        try:
            await asyncio.to_thread(ws.remove_worktree, Path(t["worktree_path"]))
        except GitError:
            pass  # dirty worktree is kept, never discarded
        self._check_milestone(milestone["id"])

    def _check_milestone(self, milestone_id: int) -> None:
        ms = row(self.conn, "SELECT * FROM milestones WHERE id=?", milestone_id)
        if ms["status"] != "ACTIVE":
            return
        c = row(self.conn, "SELECT SUM(state IN ('COMPLETED','CANCELLED')) done, SUM(state='COMPLETED') ok, COUNT(*) n "
                           "FROM tasks WHERE milestone_id=?", milestone_id)
        if c["n"] and c["done"] == c["n"] and c["ok"]:
            with tx(self.conn):
                evq.emit(self.conn, "MILESTONE_READY", project_id=ms["project_id"], milestone_id=milestone_id,
                         payload={"completed": c["ok"]}, dedupe_key=f"msready:{milestone_id}:{c['ok']}")

    # ================================================================ read helpers
    def status(self) -> dict[str, Any]:
        q = lambda sql, *a: [dict(r) for r in rows(self.conn, sql, *a)]
        return {
            "agents": q("SELECT id, provider, enabled, paused, max_concurrency, cooldown_until, cooldown_reason, "
                        "circuit_open_until FROM agents ORDER BY id"),
            "tasks_by_state": {r["state"]: r["c"] for r in rows(self.conn, "SELECT state, COUNT(*) c FROM tasks GROUP BY state")},
            "milestones": q("SELECT m.id, p.slug, m.key, m.status, m.integration_branch, m.pr_url FROM milestones m "
                            "JOIN projects p ON p.id=m.project_id ORDER BY m.id DESC LIMIT 20"),
            "pending_events": row(self.conn, "SELECT COUNT(*) c FROM events WHERE status IN ('PENDING','DEFERRED','PROCESSING')")["c"],
            "pending_approvals": row(self.conn, "SELECT COUNT(*) c FROM approval_requests WHERE status='PENDING'")["c"],
        }


def _validation_for(nt, m: Manifest) -> list[str]:
    """Code/test tasks always run the project's default suite; the TL may only add to it."""
    requested = list(nt.validation) if nt.validation is not None else list(m.validation.default)
    if nt.kind in ("code", "test"):
        requested = list(dict.fromkeys(m.validation.default + requested))
    return requested


def _check_acyclic(graph: dict[str, list[str]]) -> None:
    seen, stack = set(), set()

    def visit(n):
        if n in stack:
            raise PolicyViolation(f"dependency cycle involving {n!r}")
        if n in seen or n not in graph:
            return
        stack.add(n)
        for d in graph[n]:
            visit(d)
        stack.discard(n)
        seen.add(n)
    for k in graph:
        visit(k)


def _match(path: str, patterns: list[str]) -> bool:
    from ..policies.safety import path_matches
    return path_matches(path, patterns)


def _find_pid_by_nonce(nonce: str) -> int | None:
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if nonce in line and "agentbridge.workers.wrapper" in line:
            return int(line.split(None, 1)[0])
    return None
