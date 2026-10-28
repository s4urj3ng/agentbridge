"""Provider-reported quota: Claude rate_limit_event (shape captured from a real run), Codex (unverified shape)."""
from __future__ import annotations

import json
import time

import httpx

from agentbridge.adapters.claude import ClaudeAdapter
from agentbridge.adapters.codex import provider_quota as codex_quota
from agentbridge.db import connect, jdump, now_iso
from agentbridge.orchestrator import quota
from agentbridge.workers.process import ExitInfo

from .conftest import make_engine, q

RESET_5H = int(time.time()) + 3600
RESET_7D = int(time.time()) + 4 * 86400

# trimmed from a real `claude -p --output-format stream-json --verbose` run (Claude Code 2.1.310)
RATE_EVENT = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "resetsAt": RESET_5H, "rateLimitType": "five_hour", "overageStatus": "rejected",
    "isUsingOverage": False, "unifiedWindows": {
        "five_hour": {"utilization": 0.01, "resetsAt": RESET_5H, "status": "allowed"},
        "seven_day": {"utilization": 0.04, "resetsAt": RESET_7D, "status": "allowed"}}}}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "{\"sum\":5}",
          "structured_output": {"sum": 5}, "usage": {"input_tokens": 2, "output_tokens": 70}}


def _collect(tmp_path, events, code=0):
    (tmp_path / "stdout.log").write_text("\n".join(json.dumps(e) for e in events))
    (tmp_path / "stderr.log").write_text("")
    return ClaudeAdapter("c", {}).collect_result(tmp_path, ExitInfo(code, "exited", 2))


def test_claude_stream_reports_real_windows(tmp_path):
    res = _collect(tmp_path, [{"type": "system", "subtype": "init"}, RATE_EVENT, RESULT])
    assert res.outcome == "success" and res.structured == {"sum": 5}
    w = res.provider_quota["windows"]
    assert w["five_hour"]["used_pct"] == 1.0 and w["seven_day"]["used_pct"] == 4.0
    assert w["five_hour"]["status"] == "allowed" and w["five_hour"]["resets_at"]


def test_claude_limit_uses_provider_reset_time(tmp_path):
    ev = json.loads(json.dumps(RATE_EVENT))
    reset = int(time.time()) + 3600         # fresh: RESET_5H ages while the rest of the suite runs
    ev["rate_limit_info"]["resetsAt"] = reset
    ev["rate_limit_info"]["unifiedWindows"]["five_hour"].update(utilization=1.0, status="rejected", resetsAt=reset)
    err = dict(RESULT, is_error=True, subtype="error_during_execution", structured_output=None,
               result="Claude AI usage limit reached")
    res = _collect(tmp_path, [ev, err], code=1)
    assert res.outcome == "usage_limited"
    assert 3500 <= res.retry_after_s <= 3600      # exactly until the provider's reset, not a guess


def _set_pq(e, agent, windows, stop=None):
    e.conn.execute("UPDATE agents SET provider_quota_json=?, provider_quota_at=? WHERE id=?",
                   (jdump({"source": "test", "windows": windows}), now_iso(), agent))
    if stop is not None:
        cfg = json.loads(q(e, "SELECT config_json FROM agents WHERE id=?", agent)[0]["config_json"])
        cfg["provider_stop_pct"] = stop
        e.conn.execute("UPDATE agents SET config_json=? WHERE id=?", (jdump(cfg), agent))


def test_admission_uses_provider_report_and_falls_back(settings):
    for a in settings.agents:
        if a["id"] == "mock-worker":
            a["fallback_to"] = ["mock-worker-b"]
    e = make_engine(settings)
    future = now_iso(3600)
    _set_pq(e, "mock-worker", {"five_hour": {"used_pct": 100, "resets_at": future, "status": "rejected"}})
    ok, why = quota.admission(e.conn, "mock-worker", "work")
    assert not ok and why.startswith("provider window five_hour exhausted") and quota.is_quota_block(why)
    assert e._pick_agent("mock-worker", "work", ["mock-worker", "mock-worker-b"])[0] == "mock-worker-b"
    # owner stop level: stop before the provider refuses, to protect the weekly pool
    _set_pq(e, "mock-worker", {"seven_day": {"used_pct": 86, "resets_at": future, "status": "allowed"}},
            stop={"seven_day": 85})
    assert "seven_day at 86%" in quota.admission(e.conn, "mock-worker", "work")[1]
    # a report whose reset time has passed is stale and ignored
    _set_pq(e, "mock-worker", {"five_hour": {"used_pct": 100, "resets_at": now_iso(-60), "status": "rejected"}})
    assert quota.admission(e.conn, "mock-worker", "work")[0]


def test_codex_rate_limits_shape():
    pq = codex_quota({"primary": {"used_percent": 42.0, "window_minutes": 300, "resets_in_seconds": 1200},
                      "secondary": {"used_percent": 100, "window_minutes": 10080, "resets_in_seconds": 90000}})
    assert pq["windows"]["five_hour"]["used_pct"] == 42.0 and pq["windows"]["seven_day"]["status"] == "rejected"


def test_telegram_shows_provider_numbers(settings):
    from agentbridge.integrations.telegram import TelegramBot
    settings.agents.append({"id": "claude-x", "provider": "claude", "roles": ["worker"]})
    e = make_engine(settings)
    _set_pq(e, "claude-x", {"five_hour": {"used_pct": 12.0, "resets_at": now_iso(3600), "status": "allowed"},
                            "seven_day": {"used_pct": 40.0, "resets_at": now_iso(-5), "status": "allowed"}})
    settings.telegram = {"allowed_chat_ids": [1], "timezone": "UTC"}
    bot = TelegramBot(settings, token="1:x", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True, "result": True}))), conn=connect(settings.db_path))
    text, _ = bot.quota_text()
    assert "✅  5h" in text and "còn 88%" in text
    assert "7d đã reset" in text          # stale window is not shown as a fake number


# rate_limits exactly as found in a real Codex rollout on the N100 (codex-cli 0.164.0)
CODEX_RL = {"limit_id": "codex", "limit_name": None,
            "primary": {"used_percent": 2.0, "window_minutes": 300, "resets_at": RESET_5H},
            "secondary": {"used_percent": 11.0, "window_minutes": 10080, "resets_at": RESET_7D},
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"}, "individual_limit": None,
            "spend_control_reached": None, "plan_type": "plus", "rate_limit_reached_type": None}


def _codex_home(tmp_path, rl):
    d = tmp_path / "codex" / "sessions" / "2026" / "10" / "27"
    d.mkdir(parents=True)
    (d / "rollout-x.jsonl").write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {"id": "s"}}),
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": rl}}),
        json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}})]))
    return tmp_path / "codex"


def test_codex_quota_from_rollout(tmp_path):
    from agentbridge.adapters.codex import CodexAdapter
    ad = CodexAdapter("codex-reviewer", {"codex_home": str(_codex_home(tmp_path, CODEX_RL))})
    pq = codex_quota(ad.latest_rate_limits())
    assert pq["plan"] == "plus"
    assert pq["windows"]["five_hour"] == {"used_pct": 2.0, "resets_at": pq["windows"]["five_hour"]["resets_at"],
                                          "status": "allowed"}
    assert pq["windows"]["seven_day"]["used_pct"] == 11.0
    assert ad.latest_rate_limits(since=time.time() + 60) is None   # nothing written after `since`


def test_codex_collect_reads_rollout_and_reached_limit(tmp_path):
    from agentbridge.adapters.codex import CodexAdapter
    rl = dict(CODEX_RL, rate_limit_reached_type="primary",
              primary={"used_percent": 100.0, "window_minutes": 300, "resets_at": RESET_5H})
    ad = CodexAdapter("codex-reviewer", {"codex_home": str(_codex_home(tmp_path, rl))})
    run = tmp_path / "run"
    run.mkdir()
    (run / "stdout.log").write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}))
    (run / "stderr.log").write_text("")
    (run / "last_message.txt").write_text(json.dumps({"status": "ok", "sum": 5}))
    res = ad.collect_result(run, ExitInfo(0, "exited", 3))
    assert res.outcome == "success"
    assert res.provider_quota["windows"]["five_hour"]["status"] == "rejected"


def test_free_probe_includes_codex(settings, tmp_path):
    from agentbridge.quota_probe import refresh_free_quotas
    settings.agents.append({"id": "codex-reviewer", "provider": "codex", "roles": ["reviewer"],
                            "codex_home": str(_codex_home(tmp_path, CODEX_RL))})
    e = make_engine(settings)
    res = refresh_free_quotas(settings)
    assert res["codex-reviewer"]["windows"]["seven_day"]["used_pct"] == 11.0
    stored = json.loads(q(e, "SELECT provider_quota_json FROM agents WHERE id='codex-reviewer'")[0]["provider_quota_json"])
    assert stored["source"] == "codex rollout rate_limits"


def test_telegram_marks_agent_past_stop_level(settings):
    from agentbridge.integrations.telegram import TelegramBot
    settings.agents.append({"id": "claude-pro", "provider": "claude", "roles": ["worker"],
                            "provider_stop_pct": {"seven_day": 85}, "fallback_to": ["antigravity-argon"]})
    e = make_engine(settings)
    _set_pq(e, "claude-pro", {"seven_day": {"used_pct": 95.0, "resets_at": now_iso(80000), "status": "allowed"}})
    settings.telegram = {"allowed_chat_ids": [1], "timezone": "UTC"}
    bot = TelegramBot(settings, token="1:x", conn=connect(settings.db_path), client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": True}))))
    text, _ = bot.quota_text()
    assert "🟡 <b>claude-pro</b>" in text
    assert "đang nhường việc: tuần đã dùng 95% → antigravity-argon" in text
    assert "ngưỡng" not in text and "📐" not in text
