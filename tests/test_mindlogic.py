"""Mindlogic Gateway adapter against a local fake gateway (real subprocess + HTTP)."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentbridge.adapters.base import ExecRequest
from agentbridge.adapters.claude import ClaudeAdapter
from agentbridge.adapters.mindlogic import MindlogicAdapter

KEY = "ml-test-key-0123456789"


class FakeGateway(BaseHTTPRequestHandler):
    remaining = 7000.0
    chat_status = 200
    seen: list[str] = []

    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if self.headers.get("authorization") != f"Bearer {KEY}":
            self._send(401, {"error": "bad key"})
            return False
        return True

    def do_GET(self):
        FakeGateway.seen.append(f"GET {self.path}")
        if not self._authed():
            return
        if self.path == "/v1/gateway/credits/":
            self._send(200, {"object": "credit_balance", "total": {"quota": 8000, "used": 8000 - self.remaining,
                                                                   "remaining": self.remaining}})
        elif self.path == "/v1/gateway/models/":
            self._send(200, {"object": "list", "data": [{"id": "gpt-5.6-sol", "type": "llm"},
                                                        {"id": "claude-sonnet-5-5", "type": "llm"},
                                                        {"id": "gemini-3.8-flash-tts", "type": "audio"}]})
        else:
            self._send(404, {})

    def do_POST(self):
        FakeGateway.seen.append(f"POST {self.path}")
        if not self._authed():
            return
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n))
        if self.path != "/v1/gateway/chat/completions/":   # the gateway requires the trailing slash
            self._send(404, {})
            return
        if FakeGateway.chat_status != 200:
            self._send(FakeGateway.chat_status, {"error": "rate limited"})
            return
        assert req["model"] == "gpt-5.6-sol"
        content = json.dumps({"verdict": "approve", "summary": "fine", "issues": []})
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": content}}],
                         "usage": {"prompt_tokens": 120, "completion_tokens": 30}})


@pytest.fixture
def gateway(monkeypatch):
    FakeGateway.remaining, FakeGateway.chat_status, FakeGateway.seen = 7000.0, 200, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("MINDLOGIC_API_KEY", KEY)
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1/gateway"
    srv.shutdown()


def _run(ad, tmp_path):
    req = ExecRequest("review", "review this", tmp_path, tmp_path / "run", 60, {"type": "object"})
    return asyncio.run(ad.execute(req))


def test_detect_and_auth(gateway):
    ad = MindlogicAdapter("ml", {"base_url": gateway, "model": "gpt-5.6-sol", "credit_floor": 500})
    cap = ad.detect()
    assert cap.available and cap.verified and "3" not in (cap.version or "x")[:1]
    assert any("credits remaining: 7000.0" in n for n in cap.notes)
    assert ad.validate_auth().state == "ok"
    assert not MindlogicAdapter("ml", {"base_url": gateway, "model": "not-a-model"}).detect().available
    assert not MindlogicAdapter("ml", {"base_url": gateway, "model": "gemini-3.8-flash-tts"}).detect().available


def test_review_call_uses_trailing_slash_and_reports_usage(gateway, tmp_path):
    res = _run(MindlogicAdapter("ml", {"base_url": gateway, "model": "gpt-5.6-sol", "credit_floor": 500}), tmp_path)
    assert res.outcome == "success" and res.structured["verdict"] == "approve"
    assert res.input_tokens == 120 and res.output_tokens == 30 and "credits remaining" in res.detail
    assert "POST /v1/gateway/chat/completions/" in FakeGateway.seen


def test_credit_floor_refuses_call(gateway, tmp_path):
    FakeGateway.remaining = 100.0
    res = _run(MindlogicAdapter("ml", {"base_url": gateway, "model": "gpt-5.6-sol", "credit_floor": 500}), tmp_path)
    assert res.outcome == "usage_limited"
    assert not any(s.startswith("POST") for s in FakeGateway.seen)   # nothing was spent


def test_429_without_retry_after_is_rate_limited(gateway, tmp_path):
    FakeGateway.chat_status = 429
    res = _run(MindlogicAdapter("ml", {"base_url": gateway, "model": "gpt-5.6-sol"}), tmp_path)
    assert res.outcome == "rate_limited" and res.retry_after_s


def test_bad_key_is_auth(gateway, monkeypatch):
    monkeypatch.setenv("MINDLOGIC_API_KEY", "wrong-key-123456789")
    assert MindlogicAdapter("ml", {"base_url": gateway, "model": "gpt-5.6-sol"}).validate_auth().state == "not_logged_in"


def test_claude_gateway_mode_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDLOGIC_API_KEY", KEY)
    ad = ClaudeAdapter("claude-mindlogic-worker", {
        "api_base_url": "https://factchat-cloud.mindlogic.ai/v1/gateway/claude",
        "auth_token_env": "MINDLOGIC_API_KEY", "config_dir": str(tmp_path / "cfg")})
    inv = ad.prepare(ExecRequest("work", "p", tmp_path, tmp_path, 60, None))
    assert inv.env["ANTHROPIC_BASE_URL"].endswith("/v1/gateway/claude")
    assert inv.env["ANTHROPIC_AUTH_TOKEN"] == KEY and "ANTHROPIC_API_KEY" not in inv.env
    # a subscription identity never receives the gateway token
    assert "ANTHROPIC_AUTH_TOKEN" not in ClaudeAdapter("c", {}).prepare(
        ExecRequest("work", "p", tmp_path, tmp_path, 60, None)).env


def test_placeholder_config_is_reported(monkeypatch):
    monkeypatch.setenv("MINDLOGIC_API_KEY", KEY)
    cap = MindlogicAdapter("ml", {"base_url": "https://REPLACE-WITH-MINDLOGIC-ENDPOINT/v1", "model": "REPLACE"}).detect()
    assert not cap.available and "placeholder" in cap.notes[0]
