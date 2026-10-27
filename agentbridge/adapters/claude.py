"""Claude Code CLI adapter.

Verified on this host (Claude Code 2.1.310):
  * `claude -p` reads the prompt from stdin; `--output-format json` returns a
    single result object with `is_error`, `subtype`, `result`, `usage`,
    `total_cost_usd` and — with `--json-schema` — `structured_output`.
  * `--tools ""` disables all tools (used for the Tech Lead).
  * `--permission-prompts none` denies anything that would prompt.
  * `claude auth status --json` reports `loggedIn` / `authMethod`.
  * CLAUDE_CONFIG_DIR selects an independent config/auth state: a fresh
    directory reports loggedIn=false while the default dir is logged in.

Identity isolation: each identity gets its own `config_dir`. Log in once per
identity, interactively, on the host:
    CLAUDE_CONFIG_DIR=/srv/agentbridge/identities/claude-team-lead claude auth login
Tokens are never copied between identities by AgentBridge.

Gateway mode (e.g. Mindlogic's Anthropic-native endpoint): set
`api_base_url` and `auth_token_env` (the NAME of an env var holding the key).
The adapter then exports ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN for that
identity only. The gateway docs say Claude Code needs ANTHROPIC_AUTH_TOKEN, not
ANTHROPIC_API_KEY, with a custom base URL. Use a dedicated `config_dir` so the
identity never mixes with a subscription login. `total_cost_usd` reported by
the CLI is then a list-price estimate, not gateway credits.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..policies.safety import redact
from ..workers import process
from .base import (AgentAdapter, AgentResult, AuthStatus, Capability, ExecRequest, Invocation,
                   classify_text)

DEFAULT_WORKER_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep",
                        "Bash(git status:*)", "Bash(git diff:*)", "Bash(git add:*)", "Bash(git commit:*)",
                        "Bash(git log:*)", "Bash(python3 -m unittest:*)", "Bash(python3 -m pytest:*)"]
REVIEW_TOOLS = ["Read", "Glob", "Grep"]


class ClaudeAdapter(AgentAdapter):
    provider = "claude"
    executable_default = "claude"

    def _env(self) -> dict[str, str]:
        env = self.base_env()
        cdir = self.config.get("config_dir")
        if cdir:
            env["CLAUDE_CONFIG_DIR"] = str(Path(os.path.expanduser(cdir)))
        if self.gateway:
            env["ANTHROPIC_BASE_URL"] = self.config["api_base_url"]
            token = os.environ.get(self.config.get("auth_token_env", ""), "")
            if token:
                env["ANTHROPIC_AUTH_TOKEN"] = token
        return env

    @property
    def gateway(self) -> bool:
        return bool(self.config.get("api_base_url"))

    def detect(self) -> Capability:
        exe = self.executable
        cap = Capability(provider=self.provider, identity=self.identity, executable=exe,
                         roles_supported=["tech_lead", "worker", "reviewer"])
        if not exe:
            cap.notes.append("claude executable not found on PATH")
            return cap
        v = process.run_cmd([exe, "--version"], cwd="/", timeout_s=20, env=self._env())
        cap.version = v.stdout.strip() or None
        h = process.run_cmd([exe, "--help"], cwd="/", timeout_s=20, env=self._env())
        help_text = h.stdout + h.stderr
        cap.available = v.exit_code == 0
        cap.headless = "--print" in help_text
        cap.structured_output = "--json-schema" in help_text and "--output-format" in help_text
        cap.supports_cwd = True
        cap.can_edit_files = True
        cap.verified = cap.available and cap.headless and cap.structured_output
        if self.gateway:
            cap.notes.append(f"gateway mode: {self.config['api_base_url']} (billed to gateway credits)")
        if self.config.get("config_dir"):
            cap.isolated_identity = "verified"
            cap.notes.append("identity isolated via CLAUDE_CONFIG_DIR")
        else:
            cap.isolated_identity = "unsupported"
            cap.notes.append("no config_dir set: uses the host user's default Claude login")
        for flag in ("--tools", "--permission-prompts", "--no-session-persistence"):
            if flag not in help_text:
                cap.notes.append(f"flag {flag} not found in --help; invocation may fail")
        return cap

    def validate_auth(self) -> AuthStatus:
        exe = self.executable
        if not exe:
            return AuthStatus("error", detail="executable not found")
        if self.gateway:
            env_name = self.config.get("auth_token_env", "")
            if not os.environ.get(env_name):
                return AuthStatus("not_logged_in", method="gateway_token", detail=f"{env_name or 'auth_token_env'} not set")
            return AuthStatus("unknown", method="gateway_token", detail="token present; verify with `agents smoke`")
        r = process.run_cmd([exe, "auth", "status", "--json"], cwd="/", timeout_s=30, env=self._env())
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError:
            return AuthStatus("unknown", detail=redact((r.stderr or r.stdout)[-300:]))
        # Only non-sensitive fields are surfaced (no email/org/token).
        return AuthStatus("ok" if d.get("loggedIn") else "not_logged_in", method=d.get("authMethod"))

    def prepare(self, req: ExecRequest) -> Invocation:
        exe = self.executable or self.executable_default
        argv = [exe, "-p", "--output-format", "json", "--no-session-persistence",
                "--permission-prompts", "none", "--strict-mcp-config"]
        model = req.model or self.config.get("model")
        if model:
            argv += ["--model", model]
        if req.purpose == "tech_lead":
            argv += ["--tools", ""]
        elif req.purpose == "review":
            argv += ["--tools", ",".join(REVIEW_TOOLS)]
        else:
            tools = self.config.get("allowed_tools") or DEFAULT_WORKER_TOOLS
            argv += ["--permission-mode", self.config.get("permission_mode", "acceptEdits"),
                     "--allowedTools", ",".join(tools)]
            if self.config.get("disallowed_tools"):
                argv += ["--disallowedTools", ",".join(self.config["disallowed_tools"])]
        if req.output_schema:
            argv += ["--json-schema", json.dumps(req.output_schema, separators=(",", ":"))]
        return Invocation(argv=argv, env=self._env(), cwd=req.cwd, stdin_prompt=req.prompt)

    def collect_result(self, run_dir: Path, exit_info: process.ExitInfo | None) -> AgentResult:
        early = self.common_failure(run_dir, exit_info)
        if early:
            return early
        stdout, stderr = self.read_outputs(run_dir)
        d = self.json_from_stdout(stdout)
        dur = exit_info.duration_s if exit_info else None
        if d is None:
            outcome, retry = classify_text(stdout[-5000:] + stderr[-5000:], exit_info.exit_code if exit_info else None)
            if outcome == "success":
                outcome = "malformed"
            return AgentResult(outcome, detail=redact((stderr or stdout)[-800:]), retry_after_s=retry, duration_s=dur)
        usage = d.get("usage") or {}
        res = AgentResult(
            "success", structured=d.get("structured_output") if isinstance(d.get("structured_output"), dict) else None,
            text=str(d.get("result") or ""), duration_s=dur,
            input_tokens=_sum(usage, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
            output_tokens=usage.get("output_tokens"), cost_usd_reported=d.get("total_cost_usd"))
        if d.get("is_error") or d.get("subtype") not in (None, "success"):
            blob = json.dumps({k: d.get(k) for k in ("result", "subtype", "api_error_status", "terminal_reason")})
            outcome, retry = classify_text(blob + stderr[-3000:], 1)
            res.outcome = outcome if outcome != "success" else "failed"
            res.retry_after_s = retry
            res.detail = redact(blob[:800])
        return res


def _sum(d: dict, *keys: str) -> int | None:
    vals = [d.get(k) for k in keys if isinstance(d.get(k), int)]
    return sum(vals) if vals else None
