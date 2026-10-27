"""Common agent adapter interface.

Adapters do not run anything themselves when building an invocation: they
return an argv/env (`prepare`) which the orchestrator launches through the
crash-safe wrapper, persisting the pid before waiting. `execute()` is the
convenience path that does prepare → spawn → wait → collect in one call.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from ..policies.safety import redact, scrubbed_env
from ..workers import process

Purpose = Literal["work", "review", "tech_lead"]
Outcome = Literal["success", "failed", "blocked", "rate_limited", "usage_limited", "auth",
                  "timeout", "lost", "malformed", "unavailable"]

MAX_ARG_PROMPT = 100_000  # Linux MAX_ARG_STRLEN is 128 KiB per argument


@dataclass
class Capability:
    provider: str
    identity: str
    executable: str | None
    version: str | None = None
    available: bool = False
    headless: bool = False
    structured_output: bool = False
    supports_cwd: bool = False
    can_edit_files: bool = False
    isolated_identity: Literal["verified", "supported_unverified", "unsupported", "n/a"] = "n/a"
    verified: bool = False     # True only when features were confirmed on this host
    roles_supported: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuthStatus:
    state: Literal["ok", "not_logged_in", "unknown", "error", "n/a"]
    method: str | None = None
    detail: str = ""


@dataclass
class ExecRequest:
    purpose: Purpose
    prompt: str
    cwd: Path
    run_dir: Path
    timeout_s: float
    output_schema: dict[str, Any] | None = None
    model: str | None = None


@dataclass
class Invocation:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    stdin_prompt: str | None      # written to run_dir/prompt.txt and fed on stdin


@dataclass
class AgentResult:
    outcome: Outcome
    structured: dict[str, Any] | None = None
    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd_reported: float | None = None
    retry_after_s: int | None = None
    detail: str = ""
    duration_s: float | None = None


_RATE = re.compile(r"(rate[ _-]?limit|too many requests|\b429\b|overloaded|resource[_ ]exhausted)", re.I)
_USAGE = re.compile(r"(usage limit|quota (exceeded|exhausted)|limit reached|out of credits|insufficient[_ ]quota|weekly limit|session limit)", re.I)
_AUTH = re.compile(r"(not logged in|please (run )?/?login|invalid api key|authentication (failed|required|error)|unauthori[sz]ed|\b401\b|oauth token (has )?expired)", re.I)
_RETRY = [
    re.compile(r"retry[- ]after[:= ]+(\d+)", re.I),
    re.compile(r"try again in (\d+)\s*(s|sec|seconds|m|min|minutes|h|hours)\b", re.I),
    re.compile(r"limit reached\|(\d{10})"),  # Claude Code "…|<epoch>" format
]


def parse_retry_after(text: str) -> int | None:
    for pat in _RETRY:
        m = pat.search(text)
        if not m:
            continue
        n = int(m.group(1))
        if n > 1_000_000_000:  # epoch seconds
            return max(0, n - int(time.time()))
        unit = (m.group(2) if m.lastindex and m.lastindex >= 2 else "s").lower()
        return n * (60 if unit.startswith("m") else 3600 if unit.startswith("h") else 1)
    return None


def classify_text(text: str, exit_code: int | None) -> tuple[Outcome, int | None]:
    if _USAGE.search(text):
        return "usage_limited", parse_retry_after(text)
    if _RATE.search(text):
        return "rate_limited", parse_retry_after(text)
    if _AUTH.search(text):
        return "auth", None
    if exit_code == 0:
        return "success", None
    return "failed", None


class AgentAdapter(ABC):
    provider: str = "base"
    executable_default: str = ""

    def __init__(self, identity: str, config: dict[str, Any] | None = None):
        self.identity = identity
        self.config = dict(config or {})

    # ------------------------------------------------------------ discovery
    @property
    def executable(self) -> str | None:
        exe = self.config.get("executable") or self.executable_default
        if not exe:
            return None
        # ~/.local/bin is where the official installers put per-user CLIs, and it is
        # often missing from PATH under sudo or systemd
        return shutil.which(exe) or shutil.which(exe, path=str(Path.home() / ".local" / "bin"))

    @abstractmethod
    def detect(self) -> Capability: ...

    @abstractmethod
    def validate_auth(self) -> AuthStatus: ...

    # ------------------------------------------------------------ execution
    @abstractmethod
    def prepare(self, req: ExecRequest) -> Invocation: ...

    @abstractmethod
    def collect_result(self, run_dir: Path, exit_info: process.ExitInfo | None) -> AgentResult: ...

    def classify_failure(self, stdout: str, stderr: str, exit_code: int | None) -> tuple[Outcome, int | None]:
        return classify_text(stdout[-20000:] + "\n" + stderr[-20000:], exit_code)

    def estimate_usage(self, result: AgentResult) -> dict[str, Any]:
        return {"invocations": 1, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "duration_s": result.duration_s, "cost_usd_reported": result.cost_usd_reported,
                "tokens_reported": result.input_tokens is not None}

    def cancel(self, pid: int | None, nonce: str) -> bool:
        return process.terminate(pid, nonce)

    def base_env(self) -> dict[str, str]:
        return scrubbed_env(self.config.get("env") or {})

    async def execute(self, req: ExecRequest, on_spawn: Callable[[int, str], None] | None = None) -> AgentResult:
        inv = self.prepare(req)
        req.run_dir.mkdir(parents=True, exist_ok=True)
        if inv.stdin_prompt is not None:
            (req.run_dir / "prompt.txt").write_text(inv.stdin_prompt)
        nonce = uuid.uuid4().hex
        pid = process.spawn_detached(inv.argv, run_dir=req.run_dir, cwd=inv.cwd, nonce=nonce,
                                     timeout_s=req.timeout_s, env=inv.env)
        if on_spawn:
            on_spawn(pid, nonce)
        info = await process.wait_exit(pid, req.run_dir, nonce, time.time() + req.timeout_s + 30)
        return self.collect_result(req.run_dir, info)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def read_outputs(run_dir: Path) -> tuple[str, str]:
        def rd(n):
            p = run_dir / n
            return p.read_text(errors="replace") if p.exists() else ""
        return rd("stdout.log"), rd("stderr.log")

    def common_failure(self, run_dir: Path, info: process.ExitInfo | None) -> AgentResult | None:
        """Handle lost/timeout/spawn errors uniformly. None → adapter parses output."""
        if info is None:
            return AgentResult("lost", detail="process ended without exit record")
        if info.status == "timeout":
            return AgentResult("timeout", detail="execution timed out", duration_s=info.duration_s)
        if info.status == "spawn_error":
            _, err = self.read_outputs(run_dir)
            return AgentResult("unavailable", detail=redact(err[-500:]), duration_s=info.duration_s)
        if info.status == "killed":
            return AgentResult("failed", detail="cancelled", duration_s=info.duration_s)
        return None

    @staticmethod
    def json_from_stdout(stdout: str) -> dict | None:
        s = stdout.strip()
        if not s:
            return None
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            pass
        # stream-json / NDJSON: take the last object line
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    v = json.loads(line)
                    if isinstance(v, dict):
                        return v
                except json.JSONDecodeError:
                    continue
        return None
