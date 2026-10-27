"""Owner runtime overrides of role assignment ("⚙️ Phân công" in Telegram) against mock agents."""
from __future__ import annotations

import json

import httpx
import pytest

from agentbridge.db import connect, now_iso
from agentbridge.integrations.telegram import CFG_OFF, TelegramBot
from agentbridge.orchestrator import overrides
from agentbridge.orchestrator.engine import Engine

from .conftest import make_engine, q, register, run
from .test_telegram import OWNER, STRANGER, FakeTelegram, _cb

TG = "telegram:42"


@pytest.fixture
def setup(settings, tmp_path):
    settings.telegram = {"allowed_chat_ids": [OWNER], "timezone": "UTC"}
    settings.agents.append({"id": "mock-worker-c", "provider": "mock", "roles": ["worker"]})
    e = make_engine(settings)
    register(e, tmp_path)
    fake = FakeTelegram()
    bot = TelegramBot(settings, token="123:SECRET", client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                      conn=connect(settings.db_path))
    return e, bot, fake


def press(bot, fake, *datas, chat=OWNER):
    """Tap buttons one by one; returns (callback answers, last edited text, last keyboard)."""
    fake.calls.clear()
    for i, d in enumerate(datas):
        fake.updates = [_cb(chat, d, 100 + i)]
        bot.poll_once(timeout=0)
    answers = [b.get("text", "") for b in fake.sent("answerCallbackQuery")]
    edits = fake.sent("editMessageText")
    return answers, (edits[-1]["text"] if edits else ""), (edits[-1]["reply_markup"]["inline_keyboard"] if edits else [])


def buttons(kb):
    return {b["text"]: b["callback_data"] for r in kb for b in r}


def opt(bot, chat, agent_id):
    return f"cfg:d:{bot.drafts[chat]['options'].index(agent_id)}"


def test_flag_off_by_default_is_read_only(setup):
    e, bot, fake = setup
    assert bot.allow_config_changes is False
    answers, text, kb = press(bot, fake, "cfg", "cfg:p:0")
    assert "Phân công" in fake.sent("editMessageText")[0]["text"]
    assert "mock-worker" in text and not any(d.startswith("cfg:pe") for d in buttons(kb).values())
    answers, text, _ = press(bot, fake, "cfg:pe:0:dw", "cfg:ae:0:mc", "cfg:y")
    assert answers[0] == CFG_OFF and answers[1] == CFG_OFF
    assert q(e, "SELECT COUNT(*) c FROM overrides")[0]["c"] == 0
    # strangers get nothing at all, even with the flag on
    bot.allow_config_changes = True
    fake.calls.clear()
    fake.updates = [_cb(STRANGER, "cfg:pe:0:dw", 300)]
    bot.poll_once(timeout=0)
    assert not fake.sent("editMessageText") and not bot.drafts


def test_menu_has_assignment_button_and_short_callbacks(setup):
    e, bot, fake = setup
    from agentbridge.integrations.telegram import MAIN_MENU
    assert any(b["callback_data"] == "cfg" and "Phân công" in b["text"] for r in MAIN_MENU for b in r)
    bot.allow_config_changes = True
    for d in ("cfg", "cfg:p:0", "cfg:pe:0:wk", "cfg:a:0", "cfg:ae:0:fb"):
        _, _, kb = press(bot, fake, d)
        assert all(len(v.encode()) <= 64 for v in buttons(kb).values())


def test_confirm_then_apply_routes_work_to_new_default_worker(setup):
    e, bot, fake = setup
    bot.allow_config_changes = True
    bot.setup()
    press(bot, fake, "cfg:p:0", "cfg:pe:0:dw")
    answers, text, kb = press(bot, fake, opt(bot, OWNER, "mock-worker-b"))
    assert "Xác nhận" in text and "mock-worker → <b>mock-worker-b</b>" in text
    assert q(e, "SELECT COUNT(*) c FROM overrides")[0]["c"] == 0          # nothing applied before confirm
    answers, text, _ = press(bot, fake, "cfg:y")
    assert answers[-1] == "Đã lưu." and "mock-worker-b" in text and "✏️" in text
    a = q(e, "SELECT actor, detail_json FROM audit_log WHERE action='config.override'")
    assert a[0]["actor"] == TG and json.loads(a[0]["detail_json"])["value"] == "mock-worker-b"
    # every allowed chat is told
    assert any("Phân công thay đổi" in b["text"] and b["chat_id"] == OWNER for b in fake.sent())
    # Tech Lead roster and dispatch use the new choice
    pid = q(e, "SELECT id FROM projects")[0]["id"]
    assert e._agent_roster(e.manifest(pid))["default_worker"] == "mock-worker-b"
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "A1")
    run(e, 120)
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
    assert q(e, "SELECT COUNT(*) c FROM execution_attempts WHERE agent_id='mock-worker'")[0]["c"] == 0
    assert {t["assignee"] for t in q(e, "SELECT assignee FROM tasks")} == {"mock-worker-b"}


def test_cancel_discards_draft(setup):
    e, bot, fake = setup
    bot.allow_config_changes = True
    press(bot, fake, "cfg:pe:0:rv")
    answers, _, _ = press(bot, fake, "cfg:n", "cfg:y")
    assert answers == ["Đã huỷ.", "Phiên chỉnh đã hết, mở lại."]
    assert q(e, "SELECT COUNT(*) c FROM overrides")[0]["c"] == 0


def test_ordered_lists_and_queued_task_follows_new_order(setup, tmp_path):
    e, bot, fake = setup
    bot.allow_config_changes = True
    # mock-worker is paused: its tasks wait in QUEUED (no quota fallback for paused agents)
    e.conn.execute("UPDATE agents SET paused=1 WHERE id='mock-worker'")
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "A2")
    run(e, 20)
    assert q(e, "SELECT state FROM tasks WHERE assignee='mock-worker'")[0]["state"] == "QUEUED"
    # owner: default worker c, then b; mock-worker leaves the project
    press(bot, fake, "cfg:pe:0:dw")
    press(bot, fake, opt(bot, OWNER, "mock-worker-c"), "cfg:y")
    press(bot, fake, "cfg:pe:0:wk")
    press(bot, fake, opt(bot, OWNER, "mock-worker-c"), opt(bot, OWNER, "mock-worker-b"), "cfg:d:ok")
    answers, _, _ = press(bot, fake, "cfg:y")
    assert answers[-1] == "Đã lưu."
    assert overrides.get(e.conn, "project", "demo-calc") == {"default_worker": "mock-worker-c",
                                                             "workers": ["mock-worker-c", "mock-worker-b"]}
    run(e, 120)
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
    assert q(e, "SELECT COUNT(*) c FROM execution_attempts WHERE agent_id='mock-worker'")[0]["c"] == 0
    rr = q(e, "SELECT actor, detail_json FROM audit_log WHERE action='task.reroute'")
    assert rr and rr[0]["actor"] == "override" and '"dst": "mock-worker-c"' in rr[0]["detail_json"]


def test_fallback_order_override_is_honoured(setup):
    e, bot, fake = setup
    for a in e.s.agents:
        if a["id"] == "mock-worker":
            a.update(fail_mode="usage_limit", fail_times=99, fallback_to=["mock-worker-b"])
    e.sync_agents()
    overrides.set_override(e.conn, "project", "demo-calc", "workers", ["mock-worker-b", "mock-worker-c"], TG)
    bot.allow_config_changes = True
    idx = [a["id"] for a in bot._agent_rows()].index("mock-worker")
    press(bot, fake, f"cfg:ae:{idx}:fb")
    press(bot, fake, opt(bot, OWNER, "mock-worker-c"), opt(bot, OWNER, "mock-worker-b"), "cfg:d:ok", "cfg:y")
    assert overrides.fallback_chain(e.conn, "mock-worker") == ["mock-worker-c", "mock-worker-b"]
    e.conn.execute("UPDATE agents SET cooldown_until=?, cooldown_reason='usage_limited' WHERE id='mock-worker'",
                   (now_iso(600),))
    roster = ["mock-worker", "mock-worker-b", "mock-worker-c"]
    assert e._pick_agent("mock-worker", "work", roster)[0] == "mock-worker-c"
    e.conn.execute("UPDATE agents SET cooldown_until=NULL WHERE id='mock-worker'")
    mid = e.submit_goal("demo-calc", "Fix add()", "A3")
    run(e, 120)
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
    fb = q(e, "SELECT detail_json FROM audit_log WHERE action='task.fallback'")
    assert fb and '"dst": "mock-worker-c"' in fb[0]["detail_json"]


def test_overrides_survive_sync_and_restart_then_reset(setup):
    e, bot, fake = setup
    overrides.set_override(e.conn, "agent", "mock-worker", "max_concurrency", 0, TG)
    overrides.set_override(e.conn, "agent", "mock-worker-b", "enabled", False, TG)
    overrides.set_override(e.conn, "agent", "mock-worker", "fallback_to", ["mock-worker-c"], TG)
    overrides.set_override(e.conn, "project", "demo-calc", "reviewers", ["mock-reviewer"], TG)
    e.sync_agents()
    e2 = Engine(e.s)          # service restart
    e2.sync_agents()
    agents = {r["id"]: r for r in q(e2, "SELECT id, enabled, max_concurrency FROM agents")}
    assert agents["mock-worker"]["max_concurrency"] == 0 and agents["mock-worker-b"]["enabled"] == 0
    assert overrides.fallback_chain(e2.conn, "mock-worker") == ["mock-worker-c"]
    pid = q(e2, "SELECT id FROM projects")[0]["id"]
    assert e2._agent_roster(e2.manifest(pid))["workers"] == ["mock-worker"]   # b is disabled by override
    # reset to config from Telegram (confirm step, audited)
    bot.allow_config_changes = True
    idx = [a["id"] for a in bot._agent_rows()].index("mock-worker")
    _, text, kb = press(bot, fake, f"cfg:a:{idx}")
    assert "♻️ Về cấu hình gốc" in buttons(kb)
    _, text, _ = press(bot, fake, f"cfg:ar:{idx}")
    assert "cấu hình gốc" in text and agents["mock-worker"]["max_concurrency"] == 0
    answers, _, _ = press(bot, fake, "cfg:y")
    assert answers[-1] == "Đã về cấu hình gốc."
    assert q(e2, "SELECT max_concurrency FROM agents WHERE id='mock-worker'")[0]["max_concurrency"] == 2
    assert overrides.fallback_chain(e2.conn, "mock-worker") == []
    press(bot, fake, "cfg:pr:0", "cfg:y")
    assert overrides.get(e2.conn, "project", "demo-calc") == {}
    assert q(e2, "SELECT actor FROM audit_log WHERE action='config.reset'") == [{"actor": TG}, {"actor": TG}]


def test_invalid_choices_rejected(setup):
    e, bot, fake = setup
    e.s.agents.append({"id": "mock-both", "provider": "mock", "roles": ["worker", "reviewer"]})
    e.sync_agents()
    bad = [("project", "demo-calc", "default_worker", "ghost"),            # unknown id
           ("project", "demo-calc", "default_worker", "mock-reviewer"),    # wrong role
           ("project", "demo-calc", "reviewers", ["mock-worker"]),         # wrong role
           ("project", "demo-calc", "reviewers", []),
           ("project", "demo-calc", "workers", ["mock-worker-b", "mock-worker-b"]),
           ("project", "nope", "workers", []),
           ("agent", "mock-worker", "fallback_to", ["mock-worker"]),       # itself
           ("agent", "mock-worker", "fallback_to", ["mock-reviewer"]),     # no shared role
           ("agent", "mock-worker", "max_concurrency", 99),
           ("agent", "ghost", "enabled", True),
           ("agent", "mock-worker", "roles", ["reviewer"])]                # not configurable
    for scope, target, key, value in bad:
        with pytest.raises(ValueError):
            overrides.set_override(e.conn, scope, target, key, value, TG)
    # a reviewer can never be the task author: reviewers == [default worker] is refused
    overrides.set_override(e.conn, "project", "demo-calc", "default_worker", "mock-both", TG)
    with pytest.raises(ValueError, match="author"):
        overrides.set_override(e.conn, "project", "demo-calc", "reviewers", ["mock-both"], TG)
    e.conn.execute("UPDATE agents SET enabled=0 WHERE id='mock-worker-c'")
    with pytest.raises(ValueError, match="disabled"):
        overrides.set_override(e.conn, "project", "demo-calc", "workers", ["mock-worker-c"], TG)
    assert q(e, "SELECT COUNT(*) c FROM overrides")[0]["c"] == 1
    # forged / stale buttons and choices that became invalid before confirm
    bot.allow_config_changes = True
    answers, _, _ = press(bot, fake, "cfg:pe:0:wk", "cfg:d:99")
    assert answers[-1] == "Nút không còn hợp lệ, mở lại." and not bot.drafts
    answers, _, _ = press(bot, fake, "cfg:pe:7:wk", "cfg:zz", "cfg:ae:0:xx")
    assert answers == ["Nút không còn hợp lệ, mở lại.", "Phiên chỉnh đã hết, mở lại.", "Nút không còn hợp lệ, mở lại."]
    press(bot, fake, "cfg:pe:0:wk")
    press(bot, fake, opt(bot, OWNER, "mock-worker-b"), "cfg:d:ok")
    e.conn.execute("UPDATE agents SET enabled=0 WHERE id='mock-worker-b'")
    answers, _, _ = press(bot, fake, "cfg:y")
    assert answers[-1].startswith("Không lưu:") and "disabled" in answers[-1]
    assert "workers" not in overrides.get(e.conn, "project", "demo-calc")
