"""Mock provider adapter: runs agentbridge.adapters.mock_agent as a subprocess."""
from __future__ import annotations

import sys
from pathlib import Path

from ..policies.safety import redact
from ..workers import process
from .base import AgentAdapter, AgentResult, AuthStatus, Capability, ExecRequest, Invocation, classify_text


class MockAdapter(AgentAdapter):
    provider = "mock"

    def detect(self) -> Capability:
        return Capability(provider="mock", identity=self.identity, executable=sys.executable, version="mock-1",
                          available=True, headless=True, structured_output=True, supports_cwd=True,
                          can_edit_files=True, isolated_identity="n/a", verified=True,
                          roles_supported=["tech_lead", "worker", "reviewer"],
                          notes=["deterministic mock; consumes no subscription quota"])

    def validate_auth(self) -> AuthStatus:
        return AuthStatus("n/a", method="none")

    def prepare(self, req: ExecRequest) -> Invocation:
        c = self.config
        argv = [sys.executable, "-m", "agentbridge.adapters.mock_agent", "--purpose", req.purpose,
                "--identity", self.identity, "--scenario", c.get("scenario", "calc"),
                "--state-dir", str(c.get("state_dir", "/tmp/agentbridge-mock"))]
        if c.get("fail_mode"):
            argv += ["--fail-mode", c["fail_mode"], "--fail-times", str(c.get("fail_times", 1))]
        if c.get("script"):
            argv += ["--script", str(c["script"])]
        if req.model or c.get("model"):
            argv += ["--model", req.model or c["model"]]
        if c.get("sleep"):
            argv += ["--sleep", str(c["sleep"])]
        env = self.base_env()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        return Invocation(argv=argv, env=env, cwd=req.cwd, stdin_prompt=req.prompt)

    def collect_result(self, run_dir: Path, exit_info: process.ExitInfo | None) -> AgentResult:
        early = self.common_failure(run_dir, exit_info)
        if early:
            return early
        stdout, stderr = self.read_outputs(run_dir)
        code = exit_info.exit_code if exit_info else None
        d = self.json_from_stdout(stdout)
        dur = exit_info.duration_s if exit_info else None
        if code != 0:
            outcome, retry = classify_text(stdout + stderr, code)
            return AgentResult(outcome if outcome != "success" else "failed", retry_after_s=retry,
                               detail=redact(stderr[-500:]), duration_s=dur)
        if d is None or not isinstance(d.get("structured_output"), dict):
            return AgentResult("malformed", text=stdout[-2000:], detail="no structured output", duration_s=dur)
        return AgentResult("success", structured=d["structured_output"], text=stdout[-2000:], duration_s=dur)
