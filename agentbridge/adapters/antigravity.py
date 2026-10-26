"""Google Antigravity CLI (`agy`) adapter.

Verified on this host (agy 1.4.7, `agy --help`, one real call):
  * `agy --print <prompt>` runs one prompt non-interactively.
  * `--output-format json` returns {"status": "SUCCESS", "response": ...,
    "structured_output": {...}, "usage": {input_tokens, output_tokens, ...}}.
  * `--json-schema <schema>` constrains the final output.
  * `--mode accept-edits|auto|plan`, `--model`, `--sandbox`, `--add-dir`.
  * `agy models` lists models (used as a cheap auth probe; no prompt is sent).

Not verified: a per-identity config directory. AgentBridge therefore treats
Antigravity as a single host identity. The prompt is passed as an argument,
so it is capped below Linux's 128 KiB per-argument limit.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..policies.safety import redact
from ..workers import process
from .base import (MAX_ARG_PROMPT, AgentAdapter, AgentResult, AuthStatus, Capability, ExecRequest,
                   Invocation, classify_text)


class AntigravityAdapter(AgentAdapter):
    provider = "antigravity"
    executable_default = "agy"

    def detect(self) -> Capability:
        exe = self.executable
        cap = Capability(provider=self.provider, identity=self.identity, executable=exe,
                         roles_supported=["worker", "reviewer"], isolated_identity="unsupported")
        if not exe:
            cap.notes.append("agy executable not found; Antigravity adapter disabled")
            return cap
        h = process.run_cmd([exe, "--help"], cwd="/", timeout_s=20, env=self.base_env())
        text = h.stdout + h.stderr
        v = process.run_cmd([exe, "--version"], cwd="/", timeout_s=20, env=self.base_env())
        cap.version = v.stdout.strip().splitlines()[0] if v.stdout.strip() else None
        cap.available = h.exit_code in (0, 2) and "--print" in text
        cap.headless = "--print" in text
        cap.structured_output = "--json-schema" in text and "--output-format" in text
        cap.supports_cwd = True
        cap.can_edit_files = "--mode" in text
        cap.verified = cap.available and cap.headless and cap.structured_output
        cap.notes.append("single host identity (no verified per-identity config dir)")
        if not cap.headless:
            cap.notes.append("no headless --print mode detected: adapter must stay disabled")
        return cap

    def validate_auth(self) -> AuthStatus:
        exe = self.executable
        if not exe:
            return AuthStatus("error", detail="executable not found")
        r = process.run_cmd([exe, "models"], cwd="/", timeout_s=60, env=self.base_env())
        lines = [l for l in r.stdout.splitlines() if "\t" in l]
        if r.exit_code == 0 and lines:
            return AuthStatus("ok", method="antigravity-login", detail=f"{len(lines)} models visible")
        outcome, _ = classify_text(r.stdout + r.stderr, r.exit_code)
        return AuthStatus("not_logged_in" if outcome == "auth" else "unknown",
                          detail=redact((r.stderr or r.stdout)[-300:]))

    def prepare(self, req: ExecRequest) -> Invocation:
        exe = self.executable or self.executable_default
        prompt = req.prompt
        if len(prompt.encode()) > MAX_ARG_PROMPT:
            prompt = prompt.encode()[:MAX_ARG_PROMPT].decode(errors="ignore") + "\n[prompt truncated]"
        argv = [exe, "--print", prompt, "--output-format", "json"]
        model = req.model or self.config.get("model")
        if model:
            argv += ["--model", model]
        if req.purpose == "work":
            argv += ["--mode", self.config.get("mode", "accept-edits")]
            if self.config.get("sandbox", True):
                argv += ["--sandbox"]
        else:
            argv += ["--mode", "plan"]
        if req.output_schema:
            argv += ["--json-schema", json.dumps(req.output_schema, separators=(",", ":"))]
        return Invocation(argv=argv, env=self.base_env(), cwd=req.cwd, stdin_prompt=None)

    def collect_result(self, run_dir: Path, exit_info: process.ExitInfo | None) -> AgentResult:
        early = self.common_failure(run_dir, exit_info)
        if early:
            return early
        stdout, stderr = self.read_outputs(run_dir)
        d = self.json_from_stdout(stdout)
        dur = exit_info.duration_s if exit_info else None
        if d is None:
            outcome, retry = classify_text(stdout[-5000:] + stderr[-5000:], exit_info.exit_code if exit_info else None)
            return AgentResult("malformed" if outcome == "success" else outcome, retry_after_s=retry,
                               detail=redact((stderr or stdout)[-800:]), duration_s=dur)
        usage = d.get("usage") or {}
        res = AgentResult("success", structured=d.get("structured_output") if isinstance(d.get("structured_output"), dict) else None,
                          text=str(d.get("response") or ""), input_tokens=usage.get("input_tokens"),
                          output_tokens=usage.get("output_tokens"), duration_s=dur)
        if str(d.get("status", "")).upper() != "SUCCESS":
            outcome, retry = classify_text(json.dumps(d)[:5000] + stderr[-3000:], 1)
            res.outcome = outcome if outcome != "success" else "failed"
            res.retry_after_s = retry
            res.detail = redact(json.dumps({k: d.get(k) for k in ("status", "error")})[:800])
        return res
