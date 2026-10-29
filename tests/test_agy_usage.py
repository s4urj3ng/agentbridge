"""Antigravity /usage quota (fixture recorded from agy 1.4.7; 0 tokens per probe)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from agentbridge.adapters.antigravity import parse_usage, usage_group_for
from agentbridge.db import connect
from agentbridge.orchestrator import quota
from agentbridge.quota_probe import probe_age_s, refresh_free_quotas

from .conftest import make_engine, q

FIXTURE = Path(__file__).parent / "fixtures" / "agy_usage.json"


def test_parse_usage_groups():
    g = parse_usage(FIXTURE.read_text())
    assert set(g) == {"Gemini Models", "Claude and GPT models", "Gemini 4 Argon"}
    claude = g["Claude and GPT models"]["windows"]
    assert claude["five_hour"]["used_pct"] == 2.6 and claude["seven_day"]["used_pct"] == 1.3
    assert g["Gemini 4 Argon"]["windows"]["seven_day"]["status"] == "allowed"
    names = list(g)
    assert usage_group_for("gemini-3.8-flash-high", names) == "Gemini Models"
    assert usage_group_for("gemini-4-argon-high", names) == "Gemini 4 Argon"
    assert usage_group_for("claude-opus-5-5-medium", names) == "Claude and GPT models"
    assert parse_usage("not json") is None


def _fake_agy(tmp_path: Path, fixture: dict) -> str:
    data = tmp_path / "usage.json"
    data.write_text(json.dumps(fixture))
    exe = tmp_path / "agy"
    exe.write_text(f'#!/bin/sh\n[ "$2" = "/usage" ] || exit 2\ncat "{data}"\n')
    exe.chmod(0o755)
    return str(exe)


def test_probe_records_each_identity_and_drives_fallback(settings, tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    # make the Claude-in-Antigravity 5 h bucket nearly exhausted
    for g in fixture["command"]["data"]["groups"]:
        if g["name"] == "Claude and GPT models":
            for b in g["buckets"]:
                if b["window"] == "5h":
                    b["remaining_fraction"] = 0.05
                    b["reset_time"] = "2099-01-01T00:00:00Z"
    exe = _fake_agy(tmp_path, fixture)
    common = {"provider": "antigravity", "executable": exe}
    settings.agents += [
        {"id": "agy-flash", "roles": ["worker"], "model": "gemini-3.8-flash-high", **common},
        {"id": "agy-claude", "roles": ["reviewer"], "model": "claude-opus-5-5-medium", "allow_external_models": True,
         "provider_stop_pct": {"five_hour": 90}, "fallback_to": ["mock-reviewer"], **common},
    ]
    e = make_engine(settings)
    res = refresh_free_quotas(settings)
    assert res["agy-flash"]["group"] == "Gemini Models" and res["agy-claude"]["group"] == "Claude and GPT models"
    assert probe_age_s(e.conn) < 5
    ok, why = quota.admission(e.conn, "agy-claude", "review")
    assert not ok and "five_hour at 95%" in why
    assert e._pick_agent("agy-claude", "review", ["agy-claude", "mock-reviewer"])[0] == "mock-reviewer"
    assert quota.admission(e.conn, "agy-flash", "work")[0]


def test_telegram_quota_view_probes_antigravity(settings, tmp_path):
    from agentbridge.integrations.telegram import TelegramBot
    exe = _fake_agy(tmp_path, json.loads(FIXTURE.read_text()))
    settings.agents.append({"id": "agy-argon", "provider": "antigravity", "roles": ["worker"],
                            "model": "gemini-4-argon-high", "executable": exe})
    settings.telegram = {"allowed_chat_ids": [1], "timezone": "UTC"}
    make_engine(settings)
    bot = TelegramBot(settings, token="1:x", conn=connect(settings.db_path), client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": True}))))
    text, _ = bot.quota_text()
    assert "agy-argon" in text and "<code>7d ▓▓▓▓▓▓▓▓▓▓ còn 96%" in text


def test_usage_hang_is_bounded_and_retried(tmp_path):
    """A hanging /usage must not block: short timeout, one retry, then None."""
    import time
    from agentbridge.adapters.antigravity import AntigravityAdapter
    exe = tmp_path / "agy"
    exe.write_text("#!/bin/sh\nsleep 30\n")
    exe.chmod(0o755)
    ad = AntigravityAdapter("x", {"executable": str(exe), "model": "gemini-3.8-flash-high", "usage_timeout_s": 1})
    t0 = time.time()
    assert ad.read_usage() is None
    assert time.time() - t0 < 10


def test_run_cmd_not_blocked_by_detached_child(tmp_path):
    import time
    from agentbridge.workers.process import run_cmd
    t0 = time.time()
    r = run_cmd(["sh", "-c", "setsid sleep 30 & sleep 30"], cwd=tmp_path, timeout_s=1)
    assert r.timed_out and time.time() - t0 < 10


def test_home_dir_gives_each_account_its_own_env_settings_and_quota(tmp_path, monkeypatch):
    """Each Antigravity account lives in its own HOME; /usage is read once per account, not once per binary."""
    from agentbridge.adapters.antigravity import AntigravityAdapter, _USAGE_CACHE
    from agentbridge.adapters.base import ExecRequest
    _USAGE_CACHE.clear()
    homes = {}
    for name, frac in (("acc1", 0.9), ("acc2", 0.2)):
        h = tmp_path / name
        (h / ".gemini" / "antigravity-cli").mkdir(parents=True)
        (h / ".gemini" / "antigravity-cli" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": [f"command(echo {name})"]}}))
        fx = json.loads(FIXTURE.read_text())
        for g in fx["command"]["data"]["groups"]:
            for b in g["buckets"]:
                b["remaining_fraction"] = frac
        (h / "usage.json").write_text(json.dumps(fx))
        homes[name] = h
    exe = tmp_path / "agy"
    exe.write_text('#!/bin/sh\ncat "$HOME/usage.json"\n')   # answers from whichever HOME it runs under
    exe.chmod(0o755)
    a1 = AntigravityAdapter("agy-1", {"executable": str(exe), "model": "gemini-3.8-flash-high", "home_dir": str(homes["acc1"])})
    a2 = AntigravityAdapter("agy-2", {"executable": str(exe), "model": "gemini-3.8-flash-high", "home_dir": str(homes["acc2"])})
    assert a1.base_env()["HOME"] == str(homes["acc1"]) and a2.base_env()["HOME"] == str(homes["acc2"])
    assert a1.prepare(ExecRequest("work", "p", tmp_path, tmp_path, 60, None)).env["HOME"] == str(homes["acc1"])
    assert a1.allowed_commands() == ["echo acc1"] and a2.allowed_commands() == ["echo acc2"]
    assert a1.usage_key != a2.usage_key
    q1 = a1.quota_for(a1.read_usage_cached())["windows"]["five_hour"]["used_pct"]
    q2 = a2.quota_for(a2.read_usage_cached())["windows"]["five_hour"]["used_pct"]
    assert (q1, q2) == (10.0, 80.0)          # not the same cached number for both accounts
    plain = AntigravityAdapter("agy-0", {"executable": str(exe), "model": "gemini-3.8-flash-high"})
    assert "HOME" not in plain.base_env() or plain.base_env()["HOME"] == os.environ.get("HOME")
