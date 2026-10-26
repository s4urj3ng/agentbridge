"""Adapter parsing/classification against recorded real output shapes (no CLI calls)."""
from __future__ import annotations

import json

from agentbridge.adapters.antigravity import AntigravityAdapter
from agentbridge.adapters.base import ExecRequest, classify_text, parse_retry_after
from agentbridge.adapters.claude import ClaudeAdapter
from agentbridge.adapters.codex import CodexAdapter
from agentbridge.workers.process import ExitInfo

# Shapes captured from real runs on the dev host (claude 2.1.310, agy 1.4.7), trimmed.
CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False, "result": "{\"status\":\"ok\"}",
             "structured_output": {"status": "ok", "n": 7}, "total_cost_usd": 0.0008775,
             "usage": {"input_tokens": 2, "cache_creation_input_tokens": 4204, "cache_read_input_tokens": 0,
                       "output_tokens": 73}}
AGY_OK = {"conversation_id": "x", "status": "SUCCESS", "response": "ok", "structured_output": {"n": 7, "status": "ok"},
          "usage": {"input_tokens": 17536, "output_tokens": 23, "total_tokens": 17559}}


def _run_dir(tmp_path, stdout, stderr=""):
    (tmp_path / "stdout.log").write_text(stdout)
    (tmp_path / "stderr.log").write_text(stderr)
    return tmp_path


def test_claude_collect_success(tmp_path):
    r = ClaudeAdapter("c", {}).collect_result(_run_dir(tmp_path, json.dumps(CLAUDE_OK)), ExitInfo(0, "exited", 1.5))
    assert r.outcome == "success" and r.structured == {"status": "ok", "n": 7}
    assert r.input_tokens == 4206 and r.output_tokens == 73 and r.cost_usd_reported == 0.0008775


def test_claude_collect_usage_limit(tmp_path):
    d = dict(CLAUDE_OK, is_error=True, subtype="error_during_execution", structured_output=None,
             result="Claude AI usage limit reached|1999999999")
    r = ClaudeAdapter("c", {}).collect_result(_run_dir(tmp_path, json.dumps(d)), ExitInfo(1, "exited", 1))
    assert r.outcome == "usage_limited" and r.retry_after_s and r.retry_after_s > 0


def test_claude_invocation_shapes(tmp_path):
    ad = ClaudeAdapter("claude-team-lead", {"config_dir": "/srv/ab/identities/lead"})
    inv = ad.prepare(ExecRequest("tech_lead", "p", tmp_path, tmp_path, 60, {"type": "object"}))
    assert inv.argv[inv.argv.index("--tools") + 1] == ""
    assert "--json-schema" in inv.argv and inv.stdin_prompt == "p"
    assert inv.env["CLAUDE_CONFIG_DIR"] == "/srv/ab/identities/lead"
    w = ad.prepare(ExecRequest("work", "p", tmp_path, tmp_path, 60, None))
    assert "--permission-mode" in w.argv and "--dangerously-skip-permissions" not in w.argv
    assert "ANTHROPIC_API_KEY" not in w.env


def test_agy_collect(tmp_path):
    r = AntigravityAdapter("a", {}).collect_result(_run_dir(tmp_path, json.dumps(AGY_OK)), ExitInfo(0, "exited", 7))
    assert r.outcome == "success" and r.structured["n"] == 7 and r.input_tokens == 17536


def test_lost_and_timeout(tmp_path):
    ad = ClaudeAdapter("c", {})
    assert ad.collect_result(tmp_path, None).outcome == "lost"
    assert ad.collect_result(tmp_path, ExitInfo(-9, "timeout", 60)).outcome == "timeout"


def test_classification():
    assert classify_text("429 Too Many Requests", 1)[0] == "rate_limited"
    assert classify_text("You've hit your weekly limit", 1)[0] == "usage_limited"
    assert classify_text("Not logged in · Please run /login", 1)[0] == "auth"
    assert classify_text("boom", 2)[0] == "failed"
    assert parse_retry_after("try again in 5 minutes") == 300
    assert parse_retry_after("Retry-After: 30") == 30


def test_codex_unverified_flags_detection(monkeypatch, tmp_path):
    """Without the codex binary, detect() reports unavailable instead of inventing commands."""
    monkeypatch.setenv("PATH", str(tmp_path))
    cap = CodexAdapter("codex-reviewer", {}).detect()
    assert not cap.available and not cap.verified and "not found" in cap.notes[0]


def test_schemas_are_portable():
    """Claude Code's --json-schema strict mode rejects `discriminator` (found in a real run)."""
    from agentbridge.orchestrator.decisions import envelope_json_schema, review_result_schema, worker_result_schema
    for s in (envelope_json_schema(), worker_result_schema(), review_result_schema()):
        assert "discriminator" not in json.dumps(s)
