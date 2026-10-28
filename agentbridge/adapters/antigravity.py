"""Google Antigravity CLI (`agy`) adapter.

Verified on this host (agy 1.4.7, `agy --help`, one real call):
  * `agy --print <prompt>` runs one prompt non-interactively.
  * `--output-format json` returns {"status": "SUCCESS", "response": ...,
    "structured_output": {...}, "usage": {input_tokens, output_tokens, ...}}.
  * `--json-schema <schema>` constrains the final output.
  * `--mode accept-edits|auto|plan`, `--model`, `--sandbox`, `--add-dir`.
  * `agy models` lists models (used as a cheap auth probe; no prompt is sent).

  * `agy --print /usage --output-format json` runs the /usage slash command
    without a model turn (0 tokens) and returns the real quota as
    `command.data.groups[].buckets[]` with `window` (5h/weekly),
    `remaining_fraction` and `reset_time`. Groups seen: "Gemini Models"
    (Flash, Pro), "Claude and GPT models" (Claude Opus/Sonnet, GPT-OSS) and
    "Gemini 4 Argon".

Not verified: a per-identity config directory. AgentBridge therefore treats
Antigravity as a single host identity. The prompt is passed as an argument,
so it is capped below Linux's 128 KiB per-argument limit.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..policies.safety import PolicyViolation, redact
from ..workers import process
from .base import (MAX_ARG_PROMPT, AgentAdapter, AgentResult, AuthStatus, Capability, ExecRequest,
                   Invocation, classify_text)


EXTERNAL_MODEL_PREFIXES = ("claude-", "gpt-oss")
_USAGE_CACHE: dict[str, tuple[float, dict | None]] = {}


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
        ok, why = self.model_allowed(self.config.get("model"))
        if not ok:
            cap.available = False
            cap.notes.append(why)
        if cap.available:
            pq = self.quota_for(self.read_usage_cached())
            if pq:
                left = ", ".join(f"{k} {100 - w['used_pct']:.0f}% left" for k, w in pq["windows"].items())
                cap.notes.append(f"real quota via /usage ({pq['group']}): {left}")
            else:
                cap.notes.append("/usage quota not available for this model")
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

    def model_allowed(self, model: str | None) -> tuple[bool, str]:
        """Third-party models inside Antigravity (Claude, GPT-OSS) have a separate, small quota.

        They are refused unless the identity sets `allow_external_models: true`.
        """
        if not model:
            return False, "no model configured: set `model` (e.g. gemini-3.8-flash-high) so the quota pool is explicit"
        if not self.config.get("allow_external_models") and model.startswith(EXTERNAL_MODEL_PREFIXES):
            return False, f"model {model!r} uses Antigravity's small external-model quota; set allow_external_models to use it"
        return True, ""

    def allowed_commands(self) -> list[str] | None:
        """`command(...)` allow rules from agy's settings.json (None when it cannot be read)."""
        path = Path(self.config.get("settings_path") or Path.home() / ".gemini" / "antigravity-cli" / "settings.json")
        try:
            rules = (json.loads(path.read_text()).get("permissions") or {}).get("allow") or []
        except (OSError, ValueError, AttributeError):
            return None
        return [r[8:-1] for r in rules if isinstance(r, str) and r.startswith("command(") and r.endswith(")")]

    def command_hint(self) -> str:
        """Headless agy auto-denies any other shell command and then ends the turn with no result."""
        cmds = self.allowed_commands()
        if cmds is None:
            return ""
        listed = ", ".join(f"`{c}`" for c in cmds) or "(none)"
        return ("\n\n## Shell commands available to you\nYou run headless: a shell command outside this list is "
                f"refused and ends your turn with no result, so the task fails. Allowed: {listed}. Run ONE "
                "command per call: no newlines, no ';', no pipes into other programs, no redirections, no "
                "$VARIABLES or subshells ('&&' only between allowed commands). Use your file read/search/edit "
                "tools for everything else. Do not "
                "commit, push or install anything: AgentBridge commits your changes and runs validation itself.")

    def prepare(self, req: ExecRequest) -> Invocation:
        ok, why = self.model_allowed(req.model or self.config.get("model"))
        if not ok:
            raise PolicyViolation(why)
        exe = self.executable or self.executable_default
        prompt = req.prompt
        if req.purpose == "work":
            prompt += self.command_hint()
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
        denied = [x.get("display_name") or x.get("action") for x in d.get("denied_actions") or [] if isinstance(x, dict)]
        if denied and res.structured is None and not res.text.strip():
            # headless mode auto-denies tools it cannot prompt for; say so instead of "invalid result"
            res.outcome = "failed"
            res.detail = (f"Antigravity denied tool permission(s) {sorted(set(denied))} and produced no result; "
                          "allow them in the agentbridge user's ~/.gemini/antigravity-cli/settings.json "
                          "(permissions.allow, e.g. command(python3))")
            return res
        if str(d.get("status", "")).upper() != "SUCCESS":
            outcome, retry = classify_text(json.dumps(d)[:5000] + stderr[-3000:], 1)
            res.outcome = outcome if outcome != "success" else "failed"
            res.retry_after_s = retry
            res.detail = redact(json.dumps({k: d.get(k) for k in ("status", "error")})[:800])
        return res

    # ------------------------------------------------------------ real quota (/usage, no model turn)
    def read_usage(self) -> dict[str, dict] | None:
        """Run `/usage` and return {group name: normalised provider quota}. None if unavailable."""
        exe = self.executable
        if not exe:
            return None
        # /usage normally answers in a few seconds, but on the N100 a call occasionally hung for
        # minutes. Keep each try short and retry once rather than block detect/probes.
        for _ in range(2):
            r = process.run_cmd([exe, "--print", "/usage", "--output-format", "json"], cwd=Path.home(),
                                timeout_s=float(self.config.get("usage_timeout_s", 20)), env=self.base_env())
            if r.exit_code == 0:
                return parse_usage(r.stdout)
        return None

    def read_usage_cached(self, max_age_s: float = 60) -> dict[str, dict] | None:
        """One /usage call serves every identity sharing this executable (same host login)."""
        key = self.executable or ""
        hit = _USAGE_CACHE.get(key)
        if hit and time.time() - hit[0] < max_age_s:
            return hit[1]
        groups = self.read_usage()
        _USAGE_CACHE[key] = (time.time(), groups)
        return groups

    def quota_for(self, groups: dict[str, dict] | None) -> dict | None:
        """The /usage group that governs this identity's model."""
        if not groups:
            return None
        return groups.get(usage_group_for(self.config.get("model") or "", list(groups)))


def usage_group_for(model: str, group_names: list[str]) -> str | None:
    m = model.lower()
    def find(word: str) -> str | None:
        return next((g for g in group_names if word in g.lower()), None)
    if m.startswith(EXTERNAL_MODEL_PREFIXES):
        return find("claude") or find("gpt")
    if "argon" in m:
        return find("argon") or find("gemini models")
    if m.startswith("gemini"):
        return next((g for g in group_names if "gemini" in g.lower() and "argon" not in g.lower()), None)
    return None


def parse_usage(stdout: str) -> dict[str, dict] | None:
    d = None
    for candidate in (stdout.strip(), (stdout.strip().splitlines() or [""])[-1]):
        try:
            d = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    groups = (((d.get("command") or {}).get("data") or {}).get("groups")) if isinstance(d, dict) else None
    if not isinstance(groups, list):
        return None
    out = {}
    for g in groups:
        windows = {}
        for b in g.get("buckets") or []:
            frac = b.get("remaining_fraction")
            if not isinstance(frac, (int, float)):
                continue
            name = {"5h": "five_hour", "weekly": "seven_day"}.get(b.get("window"), str(b.get("window")))
            windows[name] = {"used_pct": round((1 - float(frac)) * 100, 1),
                             "resets_at": b.get("reset_time"),
                             "status": "rejected" if frac <= 0 else "allowed"}
        if windows:
            out[g.get("name", "?")] = {"source": "antigravity /usage", "group": g.get("name"), "windows": windows}
    return out or None

