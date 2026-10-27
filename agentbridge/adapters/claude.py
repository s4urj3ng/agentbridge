"""Claude Code CLI adapter.

Verified on this host (Claude Code 2.1.310):
  * `claude -p` reads the prompt from stdin. With `--output-format stream-json
    --verbose` it emits NDJSON events ending in a `result` event that has
    `is_error`, `subtype`, `result`, `usage`, `total_cost_usd` and, with
    `--json-schema`, `structured_output` (same shape as `--output-format json`).
  * The stream also carries `rate_limit_event` with the subscription's real
    window state: `rate_limit_info.unifiedWindows.{five_hour,seven_day}` →
    `utilization` (0..1), `resetsAt` (epoch s), `status` (allowed/rejected).
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
        argv = [exe, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                "--permission-prompts", "none", "--strict-mcp-config"]
        model = req.model or self.config.get("model")
        if model:
            argv += ["--model", model]
        if self.config.get("effort"):   # verified flag: --effort low|medium|high|xhigh|max
            argv += ["--effort", str(self.config["effort"])]
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
        events = _events(stdout)
        d = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if d is None and not events:
            d = self.json_from_stdout(stdout)   # plain --output-format json (older configs)
        pq = provider_quota(events)
        dur = exit_info.duration_s if exit_info else None
        if d is None:
            outcome, retry = classify_text(stdout[-5000:] + stderr[-5000:], exit_info.exit_code if exit_info else None)
            if outcome == "success":
                outcome = "malformed"
            return AgentResult(outcome, detail=redact((stderr or stdout)[-800:]), retry_after_s=_reset_in(pq) or retry,
                               duration_s=dur, provider_quota=pq)
        usage = d.get("usage") or {}
        res = AgentResult(
            "success", structured=d.get("structured_output") if isinstance(d.get("structured_output"), dict) else None,
            text=str(d.get("result") or ""), duration_s=dur, provider_quota=pq,
            input_tokens=_sum(usage, "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
            output_tokens=usage.get("output_tokens"), cost_usd_reported=d.get("total_cost_usd"))
        if d.get("is_error") or d.get("subtype") not in (None, "success"):
            blob = json.dumps({k: d.get(k) for k in ("result", "subtype", "api_error_status", "terminal_reason")})
            outcome, retry = classify_text(blob + stderr[-3000:], 1)
            res.outcome = outcome if outcome != "success" else "failed"
            # the provider's own reset time beats any parsed or default backoff
            res.retry_after_s = (_reset_in(pq) if res.outcome in ("usage_limited", "rate_limited") else None) or retry
            res.detail = redact(blob[:800])
        return res


def _events(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(v, dict) and "type" in v:
                out.append(v)
    return out


def provider_quota(events: list[dict]) -> dict | None:
    """Normalise the last rate_limit_event into {"source", "windows": {name: {used_pct, resets_at, status}}}."""
    info = next((e.get("rate_limit_info") for e in reversed(events) if e.get("type") == "rate_limit_event"), None)
    if not isinstance(info, dict):
        return None
    windows = {}
    for name, w in (info.get("unifiedWindows") or {}).items():
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            windows[name] = {"used_pct": round(float(w["utilization"]) * 100, 1),
                             "resets_at": _iso(w.get("resetsAt")), "status": w.get("status")}
    if not windows and info.get("rateLimitType"):
        windows[info["rateLimitType"]] = {"used_pct": None, "resets_at": _iso(info.get("resetsAt")),
                                          "status": info.get("status")}
    return {"source": "claude rate_limit_event", "status": info.get("status"), "windows": windows} if windows else None


def _iso(epoch) -> str | None:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat(timespec="seconds") if epoch else None


def _reset_in(pq: dict | None) -> int | None:
    """Seconds until the latest reset among windows the provider marked as rejected."""
    if not pq:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    resets = [datetime.fromisoformat(w["resets_at"]) for w in pq["windows"].values()
              if w.get("status") == "rejected" and w.get("resets_at")]
    return max(0, int((max(resets) - now).total_seconds())) if resets else None


def _sum(d: dict, *keys: str) -> int | None:
    vals = [d.get(k) for k in keys if isinstance(d.get(k), int)]
    return sum(vals) if vals else None
