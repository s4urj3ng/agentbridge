"""Mindlogic adapter: OpenAI-compatible chat completions over HTTP.

Disabled by default. Text-only: it cannot edit files, so it is limited to
`reviewer` and `research`-style work. It is never selected as Tech Lead unless
an identity is explicitly configured with role tech_lead AND
`allow_as_tech_lead: true`; there is no automatic fallback to it.

Credits: the owner's allowance (~8,000 credits/month) is NOT assumed to map to
tokens or money. AgentBridge only counts invocations and any token usage the
API reports; budget it with `daily_invocation_limit` / `weekly_invocation_limit`.

Config: base_url, model, api_key_env (name of env var holding the key).
The request is executed by `python -m agentbridge.adapters.http_agent` through
the same crash-safe wrapper as CLIs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..policies.safety import redact
from ..workers import process
from .base import AgentAdapter, AgentResult, AuthStatus, Capability, ExecRequest, Invocation, classify_text


class MindlogicAdapter(AgentAdapter):
    provider = "mindlogic"

    def detect(self) -> Capability:
        cap = Capability(provider=self.provider, identity=self.identity, executable=None,
                         roles_supported=["reviewer"], isolated_identity="n/a")
        base = self.config.get("base_url")
        key_env = self.config.get("api_key_env", "MINDLOGIC_API_KEY")
        cap.available = bool(base and os.environ.get(key_env))
        cap.headless = True
        cap.structured_output = True  # validated by AgentBridge; API JSON mode not assumed
        cap.can_edit_files = False
        if not base:
            cap.notes.append("base_url not configured")
        if not os.environ.get(key_env):
            cap.notes.append(f"{key_env} not set")
        cap.notes.append("UNVERIFIED: no Mindlogic endpoint/credentials were available during development")
        return cap

    def validate_auth(self) -> AuthStatus:
        key_env = self.config.get("api_key_env", "MINDLOGIC_API_KEY")
        return AuthStatus("unknown" if os.environ.get(key_env) else "not_logged_in",
                          method="api_key", detail="key presence only; not validated against the API")

    def prepare(self, req: ExecRequest) -> Invocation:
        key_env = self.config.get("api_key_env", "MINDLOGIC_API_KEY")
        env = self.base_env()
        if os.environ.get(key_env):
            env["AB_HTTP_API_KEY"] = os.environ[key_env]
        pkg_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = pkg_root
        prompt = req.prompt
        if req.output_schema:
            prompt += "\n\nReturn ONLY a JSON object matching this JSON Schema:\n" + json.dumps(req.output_schema)
        argv = [sys.executable, "-m", "agentbridge.adapters.http_agent",
                "--base-url", self.config.get("base_url", ""), "--model", req.model or self.config.get("model", "")]
        return Invocation(argv=argv, env=env, cwd=req.cwd, stdin_prompt=prompt)

    def collect_result(self, run_dir: Path, exit_info: process.ExitInfo | None) -> AgentResult:
        early = self.common_failure(run_dir, exit_info)
        if early:
            return early
        stdout, stderr = self.read_outputs(run_dir)
        d = self.json_from_stdout(stdout)
        if exit_info and exit_info.exit_code != 0 or d is None:
            outcome, retry = classify_text(stdout + stderr, exit_info.exit_code if exit_info else None)
            return AgentResult(outcome if outcome != "success" else "failed", retry_after_s=retry,
                               detail=redact(stderr[-800:]))
        usage = d.get("usage") or {}
        return AgentResult("success" if isinstance(d.get("structured"), dict) else "malformed",
                           structured=d.get("structured"), text=d.get("text", ""),
                           input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                           duration_s=exit_info.duration_s if exit_info else None)
