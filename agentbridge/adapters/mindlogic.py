"""Mindlogic (아주LLM) API Gateway adapter: OpenAI-compatible chat completions.

Gateway facts (docs.mindlogic.ai → API Gateway, checked 2026-10-27):
  * Base URL (OpenAI-compatible): https://factchat-cloud.mindlogic.ai/v1/gateway
  * One key reaches many providers' models (OpenAI, Anthropic, Gemini, xAI, ...);
    `GET /models/` lists what the key's organisation may use.
  * `GET /credits/` returns the real balance (monthly + purchased + org-granted).
  * Rate limit 120 requests / 60 s per member (429).

Text-only: it cannot edit files, so identities of this provider are reviewers.
It is never selected as Tech Lead automatically. For a file-editing worker
billed to Mindlogic credits, use the Claude adapter with `api_base_url`
pointing at the gateway's Anthropic endpoint (see claude.py).

Credits are not converted into tokens or money. Two guards apply: the usual
invocation ceilings, and an optional `credit_floor`. Below that real remaining
balance, calls are refused and reported as a usage limit, which puts the
identity into cooldown.

Config: base_url (default above), model, api_key_env (default MINDLOGIC_API_KEY),
credit_floor, max_tokens.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from ..policies.safety import redact
from ..workers import process
from .base import AgentAdapter, AgentResult, AuthStatus, Capability, ExecRequest, Invocation, classify_text
from .http_agent import endpoint

DEFAULT_BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway"


class MindlogicAdapter(AgentAdapter):
    provider = "mindlogic"

    @property
    def base_url(self) -> str:
        return self.config.get("base_url") or DEFAULT_BASE_URL

    @property
    def key_env(self) -> str:
        return self.config.get("api_key_env", "MINDLOGIC_API_KEY")

    def _get(self, path: str) -> httpx.Response:
        return httpx.get(endpoint(self.base_url, path), follow_redirects=True, timeout=30,
                         headers={"Authorization": f"Bearer {os.environ[self.key_env]}"})

    def detect(self) -> Capability:
        cap = Capability(provider=self.provider, identity=self.identity, executable=None,
                         roles_supported=["reviewer"], isolated_identity="n/a", headless=True,
                         structured_output=True, can_edit_files=False)
        model = self.config.get("model")
        if "REPLACE" in self.base_url.upper() or "REPLACE" in str(model or "").upper():
            cap.notes.append(f"base_url/model still the example placeholder ({self.base_url}); "
                             f"use {DEFAULT_BASE_URL} and a model id from GET /models/")
            return cap
        if not os.environ.get(self.key_env):
            cap.notes.append(f"{self.key_env} not set")
            return cap
        if not model:
            cap.notes.append("model not configured")
            return cap
        try:
            r = self._get("models")
        except httpx.HTTPError as e:
            cap.notes.append(f"gateway {self.base_url} unreachable: {redact(str(e))[:200]}")
            return cap
        if r.status_code != 200:
            cap.notes.append(f"GET /models/ → HTTP {r.status_code}")
            return cap
        ids = {m.get("id") for m in r.json().get("data", []) if m.get("type", "llm") == "llm"}
        cap.version = f"{len(ids)} LLM models available"
        if model not in ids:
            cap.notes.append(f"model {model!r} is not in this key's /models list")
            return cap
        cap.available = True
        cap.verified = True
        bal = self.credit_balance()
        if bal is not None:
            cap.notes.append(f"credits remaining: {bal:.1f}"
                             + (f" (floor {self.config['credit_floor']})" if self.config.get("credit_floor") else ""))
        return cap

    def credit_balance(self) -> float | None:
        try:
            r = self._get("credits")
            return float(r.json()["total"]["remaining"]) if r.status_code == 200 else None
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None

    def validate_auth(self) -> AuthStatus:
        if not os.environ.get(self.key_env):
            return AuthStatus("not_logged_in", method="api_key", detail=f"{self.key_env} not set")
        try:
            r = self._get("credits")
        except httpx.HTTPError as e:
            return AuthStatus("unknown", method="api_key", detail=redact(str(e))[:200])
        if r.status_code == 200:
            return AuthStatus("ok", method="api_key")
        if r.status_code in (401, 403):
            return AuthStatus("not_logged_in", method="api_key", detail=f"HTTP {r.status_code}: key rejected")
        return AuthStatus("unknown", method="api_key", detail=f"HTTP {r.status_code}")

    def prepare(self, req: ExecRequest) -> Invocation:
        env = self.base_env()
        if os.environ.get(self.key_env):
            env["AB_HTTP_API_KEY"] = os.environ[self.key_env]
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        prompt = req.prompt
        if req.output_schema:
            prompt += "\n\nReturn ONLY a JSON object matching this JSON Schema:\n" + json.dumps(req.output_schema)
        argv = [sys.executable, "-m", "agentbridge.adapters.http_agent",
                "--base-url", self.base_url, "--model", req.model or self.config.get("model", "")]
        if self.config.get("credit_floor") is not None:
            argv += ["--credit-floor", str(self.config["credit_floor"])]
        if self.config.get("max_tokens"):
            argv += ["--max-tokens", str(self.config["max_tokens"])]
        return Invocation(argv=argv, env=env, cwd=req.cwd, stdin_prompt=prompt)

    def collect_result(self, run_dir: Path, exit_info: process.ExitInfo | None) -> AgentResult:
        early = self.common_failure(run_dir, exit_info)
        if early:
            return early
        stdout, stderr = self.read_outputs(run_dir)
        d = self.json_from_stdout(stdout)
        if (exit_info and exit_info.exit_code != 0) or d is None:
            outcome, retry = classify_text(stdout + stderr, exit_info.exit_code if exit_info else None)
            if "HTTP 429" in stderr and outcome in ("failed", "rate_limited"):
                outcome, retry = "rate_limited", retry or 60   # gateway may omit Retry-After
            return AgentResult(outcome if outcome != "success" else "failed", retry_after_s=retry,
                               detail=redact(stderr[-800:]))
        usage = d.get("usage") or {}
        detail = f"credits remaining {d['credits_remaining']:.1f}" if d.get("credits_remaining") is not None else ""
        return AgentResult("success" if isinstance(d.get("structured"), dict) else "malformed",
                           structured=d.get("structured"), text=d.get("text", ""), detail=detail,
                           input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                           duration_s=exit_info.duration_s if exit_info else None)
