"""OpenAI Codex CLI adapter (secondary worker / independent reviewer).

STATUS: UNVERIFIED on this host — the `codex` CLI is not installed here (the
"Codex Web GPT" desktop app's bundled binary is not the Codex CLI and is not
used). The invocation follows the documented non-interactive mode:
    codex exec --json --skip-git-repo-check --sandbox <mode> \
               --output-schema <file> --output-last-message <file> -
`detect()` checks `codex exec --help` for each flag before marking the adapter
usable, so a CLI without these flags is reported as unsupported rather than
invoked with invented arguments.

Identity isolation: CODEX_HOME selects the config/auth directory.
Codex is quota-constrained: keep `reserved_review_invocations` and low daily
limits; it is never the default reviewer (routing is explicit per manifest).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..policies.safety import redact
from ..workers import process
from .base import (AgentAdapter, AgentResult, AuthStatus, Capability, ExecRequest, Invocation,
                   classify_text)

REQUIRED_FLAGS = ("--json", "--sandbox", "--output-schema", "--output-last-message", "--skip-git-repo-check")


class CodexAdapter(AgentAdapter):
    provider = "codex"
    executable_default = "codex"

    def _env(self) -> dict[str, str]:
        env = self.base_env()
        if self.config.get("codex_home"):
            env["CODEX_HOME"] = os.path.expanduser(self.config["codex_home"])
        return env

    def detect(self) -> Capability:
        exe = self.executable
        cap = Capability(provider=self.provider, identity=self.identity, executable=exe,
                         roles_supported=["worker", "reviewer"],
                         isolated_identity="supported_unverified" if self.config.get("codex_home") else "unsupported")
        if not exe:
            cap.notes.append("codex CLI not found on PATH (install: npm i -g @openai/codex); adapter disabled")
            return cap
        v = process.run_cmd([exe, "--version"], cwd="/", timeout_s=20, env=self._env())
        cap.version = v.stdout.strip() or None
        h = process.run_cmd([exe, "exec", "--help"], cwd="/", timeout_s=20, env=self._env())
        text = h.stdout + h.stderr
        missing = [f for f in REQUIRED_FLAGS if f not in text]
        cap.available = v.exit_code == 0
        cap.headless = h.exit_code == 0 and not missing
        cap.structured_output = "--output-schema" in text
        cap.supports_cwd = "--cd" in text or "-C" in text
        cap.can_edit_files = "--sandbox" in text
        cap.verified = cap.available and cap.headless
        if missing:
            cap.notes.append(f"codex exec lacks flags {missing}; adapter disabled")
        return cap

    def validate_auth(self) -> AuthStatus:
        exe = self.executable
        if not exe:
            return AuthStatus("error", detail="executable not found")
        r = process.run_cmd([exe, "login", "status"], cwd="/", timeout_s=30, env=self._env())
        txt = (r.stdout + r.stderr).lower()
        if r.exit_code == 0 and "logged in" in txt and "not logged in" not in txt:
            return AuthStatus("ok", method="chatgpt" if "chatgpt" in txt else "api_key" if "api key" in txt else None)
        return AuthStatus("not_logged_in" if "not logged in" in txt else "unknown", detail=redact(txt[-200:]))

    def prepare(self, req: ExecRequest) -> Invocation:
        exe = self.executable or self.executable_default
        sandbox = "workspace-write" if req.purpose == "work" else "read-only"
        argv = [exe, "exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox,
                "--output-last-message", str(req.run_dir / "last_message.txt")]
        if req.output_schema:
            schema_file = req.run_dir / "output_schema.json"
            req.run_dir.mkdir(parents=True, exist_ok=True)
            schema_file.write_text(json.dumps(req.output_schema))
            argv += ["--output-schema", str(schema_file)]
        model = req.model or self.config.get("model")
        if model:
            argv += ["--model", model]
        if self.config.get("reasoning_effort"):   # codex config key, passed via the verified -c override
            argv += ["-c", f"model_reasoning_effort={self.config['reasoning_effort']}"]
        argv.append("-")  # read prompt from stdin
        return Invocation(argv=argv, env=self._env(), cwd=req.cwd, stdin_prompt=req.prompt)

    def collect_result(self, run_dir: Path, exit_info: process.ExitInfo | None) -> AgentResult:
        early = self.common_failure(run_dir, exit_info)
        if early:
            return early
        stdout, stderr = self.read_outputs(run_dir)
        dur = exit_info.duration_s if exit_info else None
        code = exit_info.exit_code if exit_info else None
        in_tok = out_tok = None
        rate_limits = None
        for line in stdout.splitlines():  # NDJSON events; pick up token usage when reported
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            u = ev.get("usage")
            if isinstance(u, dict):
                in_tok = u.get("input_tokens", in_tok)
                out_tok = u.get("output_tokens", out_tok)
            rl = _find_rate_limits(ev)
            if rl:
                rate_limits = rl
        pq = provider_quota(rate_limits)
        if code != 0:
            outcome, retry = classify_text(stdout[-5000:] + stderr[-5000:], code)
            return AgentResult(outcome if outcome != "success" else "failed", retry_after_s=retry,
                               detail=redact(stderr[-800:]), duration_s=dur, provider_quota=pq)
        last = run_dir / "last_message.txt"
        text = last.read_text() if last.exists() else ""
        structured = None
        try:
            v = json.loads(text)
            structured = v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            pass
        return AgentResult("success" if structured is not None else "malformed", structured=structured, text=text,
                           input_tokens=in_tok, output_tokens=out_tok, duration_s=dur, provider_quota=pq)


def _find_rate_limits(obj, depth: int = 0) -> dict | None:
    """Locate a `rate_limits` object with primary/secondary windows anywhere in an event.

    UNVERIFIED shape (to be confirmed against codex-cli output on the server):
    {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_in_seconds": 3600}, "secondary": {...}}
    """
    if depth > 5 or not isinstance(obj, dict):
        return None
    rl = obj.get("rate_limits")
    if isinstance(rl, dict) and any(isinstance(rl.get(k), dict) for k in ("primary", "secondary")):
        return rl
    for v in obj.values():
        if isinstance(v, dict):
            found = _find_rate_limits(v, depth + 1)
            if found:
                return found
    return None


def provider_quota(rl: dict | None) -> dict | None:
    if not rl:
        return None
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    windows = {}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            continue
        minutes = w.get("window_minutes")
        name = {300: "five_hour", 10080: "seven_day"}.get(minutes, f"{key}_{minutes}m")
        resets = None
        if isinstance(w.get("resets_in_seconds"), (int, float)):
            resets = (now + timedelta(seconds=w["resets_in_seconds"])).isoformat(timespec="seconds")
        elif isinstance(w.get("resets_at"), (int, float)):
            resets = datetime.fromtimestamp(w["resets_at"], timezone.utc).isoformat(timespec="seconds")
        used = float(w["used_percent"])
        windows[name] = {"used_pct": round(used, 1), "resets_at": resets,
                         "status": "rejected" if used >= 100 else "allowed"}
    return {"source": "codex rate_limits", "windows": windows} if windows else None
