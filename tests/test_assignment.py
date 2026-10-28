"""Owner runtime overrides of role assignment ("⚙️ Phân công" in Telegram) against mock agents."""
from __future__ import annotations

import httpx
import pytest

from agentbridge.db import connect, now_iso, tx
from agentbridge.integrations.telegram import CFG_OFF, MAIN_MENU, TelegramBot
from agentbridge.orchestrator import overrides
from agentbridge.orchestrator.engine import Engine

from .conftest import make_engine, q, register, run
from .test_telegram import OWNER, STRANGER, FakeTelegram, _cb

TG = "telegram:42"


@pytest.fixture
def setup(settings, tmp_path):
    settings.telegram = {"allowed_chat_ids": [OWNER], "timezone": "UTC"}
    settings.agents.append({"id": "mock-worker-c", "provider": "mock", "roles": ["worker"]})
    settings.agents.append({"id": "mock-reviewer-2", "provider": "mock", "roles": ["reviewer"]})
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


def cbs(kb):
    return [b["callback_data"] for r in kb for b in r]


def ai(bot, agent_id):
    return bot._agent_ids().index(agent_id)


def lists(e):
    p = overrides.effective_project(e.conn, "demo-calc")
    return p["workers"], p["reviewers"]


def test_flag_off_by_default_is_read_only(setup):
    e, bot, fake = setup
    assert bot.allow_config_changes is False
    assert any(b["callback_data"] == "cfg" and "Phân công" in b["text"] for r in MAIN_MENU for b in r)
    _, text, kb = press(bot, fake, "cfg")          # one project: opens it directly
    assert "Phân công · demo-calc" in text and "1. mock-worker" in text and "Tech Lead: mock-lead" in text
    assert not any(d.startswith(("cl:", "ca:", "cu:", "cr:")) for d in cbs(kb))
    answers, _, _ = press(bot, fake, f"cl:0:w:{ai(bot, 'mock-worker')}:d", "ca:0:w", "cu:0", "cr:0",
                          f"cg:0:{ai(bot, 'mock-worker')}:en")
    assert answers == [CFG_OFF] * 5
    assert q(e, "SELECT COUNT(*) c FROM overrides")[0]["c"] == 0
    bot.allow_config_changes = True                # strangers get nothing at all, even with the flag on
    fake.calls.clear()
    fake.updates = [_cb(STRANGER, f"cl:0:w:{ai(bot, 'mock-worker')}:d", 300)]
    bot.poll_once(timeout=0)
    assert not fake.sent("editMessageText") and q(e, "SELECT COUNT(*) c FROM overrides")[0]["c"] == 0


def test_one_screen_reorder_add_remove_undo_reset(setup):
    e, bot, fake = setup
    bot.allow_config_changes = True
    _, text, kb = press(bot, fake, "cfg")
    assert all(len(d.encode()) <= 64 for d in cbs(kb))
    assert lists(e) == (["mock-worker", "mock-worker-b"], ["mock-reviewer"])
    w, wb = ai(bot, "mock-worker"), ai(bot, "mock-worker-b")
    # ⬇️ applies at once: the message shows the new order, audited as telegram:<user id>
    answers, text, _ = press(bot, fake, f"cl:0:w:{w}:d")
    assert answers == ["Đã lưu."] and lists(e)[0] == ["mock-worker-b", "mock-worker"]
    assert text.index("1. mock-worker-b") < text.index("2. mock-worker")
    assert q(e, "SELECT actor FROM audit_log WHERE action='config.override'") == [{"actor": TG}]
    press(bot, fake, f"cl:0:w:{w}:u")
    assert lists(e)[0] == ["mock-worker", "mock-worker-b"]
    # ➕ picker lists enabled workers not yet in the list; one tap appends
    _, text, kb = press(bot, fake, "ca:0:w")
    assert [d for d in cbs(kb) if d.startswith("ca:")] == [f"ca:0:w:{ai(bot, 'mock-worker-c')}"]
    press(bot, fake, f"ca:0:w:{ai(bot, 'mock-worker-c')}")
    assert lists(e)[0] == ["mock-worker", "mock-worker-b", "mock-worker-c"]
    press(bot, fake, f"cl:0:w:{wb}:x")
    assert lists(e)[0] == ["mock-worker", "mock-worker-c"]
    # ↩️ undoes one change at a time
    answers, _, _ = press(bot, fake, "cu:0")
    assert answers == ["Đã hoàn tác."] and lists(e)[0] == ["mock-worker", "mock-worker-b", "mock-worker-c"]
    # the last agent of a role cannot be removed
    answers, _, _ = press(bot, fake, f"cl:0:r:{ai(bot, 'mock-reviewer')}:x")
    assert answers == ["Không thể bỏ agent cuối cùng của vai trò này."] and lists(e)[1] == ["mock-reviewer"]
    press(bot, fake, f"ca:0:r:{ai(bot, 'mock-reviewer-2')}", f"cl:0:r:{ai(bot, 'mock-reviewer-2')}:u")
    assert lists(e)[1] == ["mock-reviewer-2", "mock-reviewer"]
    # ♻️ drops the project override (and can itself be undone)
    answers, text, _ = press(bot, fake, "cr:0")
    assert answers == ["Đã về cấu hình gốc."] and overrides.get(e.conn, "project", "demo-calc") == {}
    assert lists(e) == (["mock-worker", "mock-worker-b"], ["mock-reviewer"]) and "✏️" not in text
    press(bot, fake, "cu:0")
    assert lists(e)[1] == ["mock-reviewer-2", "mock-reviewer"]
    # per-tap changes from Telegram are not re-broadcast (the screen itself shows the result)
    assert not [t for t, _ in bot.collect_notifications() if "Phân công" in t]


def test_new_first_worker_gets_the_work(setup):
    e, bot, fake = setup
    bot.allow_config_changes = True
    press(bot, fake, f"cl:0:w:{ai(bot, 'mock-worker-b')}:u")
    pid = q(e, "SELECT id FROM projects")[0]["id"]
    assert e._agent_roster(e.manifest(pid))["default_worker"] == "mock-worker-b"
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "A1")
    run(e, 120)
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
    # the mock Tech Lead gives the first task to list item 1 and the second to item 2
    assert q(e, "SELECT key, assignee FROM tasks ORDER BY id") == [
        {"key": "fix-add", "assignee": "mock-worker-b"}, {"key": "impl-mul", "assignee": "mock-worker"}]


def test_fallback_follows_list_order(setup):
    e, bot, fake = setup
    # YAML says mock-worker falls back to b; the owner's list says c comes next
    for a in e.s.agents:
        if a["id"] == "mock-worker":
            a["fallback_to"] = ["mock-worker-b"]
    e.sync_agents()
    roster = ["mock-worker", "mock-worker-b", "mock-worker-c"]
    e.conn.execute("UPDATE agents SET cooldown_until=?, cooldown_reason='usage_limited' WHERE id='mock-worker'",
                   (now_iso(600),))
    assert e._pick_agent("mock-worker", "work", roster)[0] == "mock-worker-b"          # no override: YAML
    overrides.set_override(e.conn, "project", "demo-calc", "workers", ["mock-worker", "mock-worker-c", "mock-worker-b"], TG)
    order = ["mock-worker", "mock-worker-c", "mock-worker-b"]
    assert e._pick_agent("mock-worker", "work", order, order)[0] == "mock-worker-c"
    # wraps round to agents above
    e.conn.execute("UPDATE agents SET cooldown_until=?, cooldown_reason='usage_limited' WHERE id='mock-worker-b'",
                   (now_iso(600),))
    assert e._pick_agent("mock-worker-b", "work", order, order)[0] == "mock-worker-c"
    # end to end: the quota-blocked first item hands the task to the second item
    e.conn.execute("UPDATE agents SET cooldown_until=NULL")
    for a in e.s.agents:
        if a["id"] == "mock-worker":
            a.update(fail_mode="usage_limit", fail_times=99)
    e.sync_agents()
    mid = e.submit_goal("demo-calc", "Fix add()", "A2")
    run(e, 120)
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
    fb = q(e, "SELECT detail_json FROM audit_log WHERE action='task.fallback'")
    assert fb and '"dst": "mock-worker-c"' in fb[0]["detail_json"]


def test_review_fallback_follows_list_and_skips_author(setup):
    e, bot, fake = setup
    e.s.agents.append({"id": "mock-both", "provider": "mock", "roles": ["worker", "reviewer"]})
    e.sync_agents()
    overrides.set_override(e.conn, "project", "demo-calc", "workers", ["mock-both", "mock-worker"], TG)
    overrides.set_override(e.conn, "project", "demo-calc", "reviewers",
                           ["mock-reviewer", "mock-both", "mock-reviewer-2"], TG)
    pid = q(e, "SELECT id FROM projects")[0]["id"]
    roster = e._agent_roster(e.manifest(pid))["reviewers"]
    order = [r for r in roster if r != "mock-both"]               # task authored by mock-both
    e.conn.execute("UPDATE agents SET cooldown_until=?, cooldown_reason='usage_limited' WHERE id='mock-reviewer'",
                   (now_iso(600),))
    assert e._pick_agent("mock-reviewer", "review", order, order)[0] == "mock-reviewer-2"


def test_queued_task_follows_new_list_when_agent_removed(setup):
    e, bot, fake = setup
    bot.allow_config_changes = True
    e.conn.execute("UPDATE agents SET paused=1 WHERE id='mock-worker'")   # its tasks wait in QUEUED
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "A3")
    run(e, 20)
    assert q(e, "SELECT state FROM tasks WHERE assignee='mock-worker'")[0]["state"] == "QUEUED"
    press(bot, fake, f"cl:0:w:{ai(bot, 'mock-worker')}:x")
    run(e, 120)
    assert q(e, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
    rr = q(e, "SELECT actor, detail_json FROM audit_log WHERE action='task.reroute'")
    assert rr and rr[0]["actor"] == "override" and '"dst": "mock-worker-b"' in rr[0]["detail_json"]


def test_agent_screen_enable_concurrency_survive_restart(setup):
    e, bot, fake = setup
    bot.allow_config_changes = True
    a = ai(bot, "mock-worker")
    _, text, kb = press(bot, fake, f"cg:0:{a}")
    assert "Số việc song song" in text and f"cg:0:{a}:en" in cbs(kb)
    press(bot, fake, f"cg:0:{a}:mc")
    answers, text, _ = press(bot, fake, f"cg:0:{a}:mc:0")
    assert answers == ["Đã lưu."] and "song song ✏️: 0" in text
    press(bot, fake, f"cg:0:{ai(bot, 'mock-worker-b')}:en")
    e.sync_agents()
    e2 = Engine(e.s)          # service restart
    e2.sync_agents()
    ag = {r["id"]: r for r in q(e2, "SELECT id, enabled, max_concurrency FROM agents")}
    assert ag["mock-worker"]["max_concurrency"] == 0 and ag["mock-worker-b"]["enabled"] == 0
    _, text, _ = press(bot, fake, "cs:0")
    assert "mock-worker-b ⏸" in text
    answers, _, _ = press(bot, fake, f"cg:0:{a}:un")
    assert answers == ["Đã hoàn tác."] and q(e2, "SELECT max_concurrency FROM agents WHERE id='mock-worker'")[0][
        "max_concurrency"] == 2
    press(bot, fake, f"cg:0:{ai(bot, 'mock-worker-b')}:rs")
    assert q(e2, "SELECT enabled FROM agents WHERE id='mock-worker-b'")[0]["enabled"] == 1


def test_legacy_default_worker_override_still_applies(setup):
    e, bot, fake = setup
    with tx(e.conn):
        e.conn.execute("INSERT INTO overrides VALUES ('project','demo-calc','default_worker','\"mock-worker-b\"',?,?)",
                       (TG, now_iso()))
    assert lists(e)[0] == ["mock-worker-b", "mock-worker"]
    assert overrides.has_role_order(e.conn, "demo-calc", "worker")
    overrides.edit_list(e.conn, "demo-calc", "worker", "down", "mock-worker-b", TG)
    assert overrides.get(e.conn, "project", "demo-calc") == {"workers": ["mock-worker", "mock-worker-b"]}


def test_invalid_choices_rejected(setup):
    e, bot, fake = setup
    e.s.agents.append({"id": "mock-both", "provider": "mock", "roles": ["worker", "reviewer"]})
    e.sync_agents()
    bad = [("project", "demo-calc", "workers", ["ghost"]),               # unknown id
           ("project", "demo-calc", "workers", ["mock-reviewer"]),       # wrong role
           ("project", "demo-calc", "reviewers", ["mock-worker"]),       # wrong role
           ("project", "demo-calc", "reviewers", []),                    # empty role
           ("project", "demo-calc", "workers", ["mock-worker-b", "mock-worker-b"]),
           ("project", "demo-calc", "default_worker", "mock-worker-b"),  # legacy key no longer written
           ("project", "nope", "workers", ["mock-worker"]),
           ("agent", "mock-worker", "max_concurrency", 99),
           ("agent", "ghost", "enabled", True),
           ("agent", "mock-worker", "roles", ["reviewer"])]              # not configurable
    for scope, target, key, value in bad:
        with pytest.raises(ValueError):
            overrides.set_override(e.conn, scope, target, key, value, TG)
    with pytest.raises(ValueError):
        overrides.edit_list(e.conn, "demo-calc", "reviewer", "remove", "mock-reviewer", TG)
    # a reviewer can never be the task author: reviewers == [default worker] is refused
    overrides.set_override(e.conn, "project", "demo-calc", "workers", ["mock-both"], TG)
    with pytest.raises(ValueError, match="author"):
        overrides.set_override(e.conn, "project", "demo-calc", "reviewers", ["mock-both"], TG)
    e.conn.execute("UPDATE agents SET enabled=0 WHERE id='mock-worker-c'")
    with pytest.raises(ValueError, match="disabled"):
        overrides.edit_list(e.conn, "demo-calc", "worker", "add", "mock-worker-c", TG)
    assert q(e, "SELECT COUNT(*) c FROM overrides")[0]["c"] == 1
    # forged / stale buttons
    bot.allow_config_changes = True
    answers, _, _ = press(bot, fake, "cl:0:w:99:u", "cs:7", "cl:0:w:x:u", "cg:0:0:zz", "ca:0:w:99")
    assert all(a == "Nút không còn hợp lệ, mở lại." for a in answers), answers
    assert q(e, "SELECT COUNT(*) c FROM overrides")[0]["c"] == 1
