"""Official Gemini CLI adapter (optional, separately authenticated).

STATUS: UNVERIFIED on this host — `gemini` is not installed. Invocation uses
the documented headless flags `-p` and `--output-format json`; `detect()`
confirms them in `gemini --help` before the adapter can be enabled. The Gemini
CLI has no JSON-schema flag, so structured output is parsed from the response
text (the prompt carries the schema) and validated by AgentBridge.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..orchestrator.decisions import _extract_json  # tolerant JSON extraction
from ..policies.safety import redact
from ..workers import process
from .base import (MAX_ARG_PROMPT, AgentAdapter, AgentResult, AuthStatus, Capability, ExecRequest,
                   Invocation, classify_text)


class GeminiCliAdapter(AgentAdapter):
    provider = "gemini"
    executable_default = "gemini"

    def detect(self) -> Capability:
        exe = self.executable
        cap = Capability(provider=self.provider, identity=self.identity, executable=exe,
                         roles_supported=["worker", "reviewer"], isolated_identity="unsupported")
        if not exe:
            cap.notes.append("gemini CLI not found on PATH; adapter disabled")
            return cap
        h = process.run_cmd([exe, "--help"], cwd="/", timeout_s=20, env=self.base_env())
        text = h.stdout + h.stderr
        v = process.run_cmd([exe, "--version"], cwd="/", timeout_s=20, env=self.base_env())
        cap.version = v.stdout.strip() or None
        cap.available = v.exit_code == 0
        cap.headless = "--prompt" in text or " -p" in text
        cap.structured_output = "--output-format" in text
        cap.supports_cwd = True
        cap.can_edit_files = "--approval-mode" in text or "--yolo" in text
        cap.verified = cap.available and cap.headless
        return cap

    def validate_auth(self) -> AuthStatus:
        return AuthStatus("unknown", detail="Gemini CLI has no verified non-interactive auth-status command")

    def prepare(self, req: ExecRequest) -> Invocation:
        exe = self.executable or self.executable_default
        prompt = req.prompt
        if req.output_schema:
            prompt += "\n\nReturn ONLY a JSON object matching this JSON Schema:\n" + json.dumps(req.output_schema)
        if len(prompt.encode()) > MAX_ARG_PROMPT:
            prompt = prompt.encode()[:MAX_ARG_PROMPT].decode(errors="ignore")
        argv = [exe, "-p", prompt, "--output-format", "json"]
        if req.purpose == "work":
            argv += ["--approval-mode", self.config.get("approval_mode", "auto_edit")]
        if req.model or self.config.get("model"):
            argv += ["--model", req.model or self.config["model"]]
        return Invocation(argv=argv, env=self.base_env(), cwd=req.cwd, stdin_prompt=None)

    def collect_result(self, run_dir: Path, exit_info: process.ExitInfo | None) -> AgentResult:
        early = self.common_failure(run_dir, exit_info)
        if early:
            return early
        stdout, stderr = self.read_outputs(run_dir)
        d = self.json_from_stdout(stdout)
        code = exit_info.exit_code if exit_info else None
        if d is None or code != 0:
            outcome, retry = classify_text(stdout[-5000:] + stderr[-5000:], code)
            return AgentResult(outcome if outcome != "success" else "malformed", retry_after_s=retry,
                               detail=redact(stderr[-800:]))
        text = str(d.get("response") or "")
        try:
            structured = _extract_json(text)
        except Exception:
            structured = None
        return AgentResult("success" if isinstance(structured, dict) else "malformed",
                           structured=structured if isinstance(structured, dict) else None, text=text,
                           duration_s=exit_info.duration_s if exit_info else None)
