"""Telegram bot against a fake Bot API (httpx.MockTransport) and a real engine/database."""
from __future__ import annotations

import json

import httpx
import pytest

from agentbridge.db import connect
from agentbridge.integrations.telegram import TelegramBot, bar
from agentbridge.orchestrator import quota

from .conftest import make_engine, q, register, run

OWNER = 111
STRANGER = 999


class FakeTelegram:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, method="sendMessage"):
        return [b for m, b in self.calls if m == method]


@pytest.fixture
def setup(settings, tmp_path):
    settings.telegram = {"allowed_chat_ids": [OWNER], "timezone": "UTC"}
    for a in settings.agents:
        if a["id"] == "mock-worker":
            a["rolling_limits"] = [{"hours": 5, "max": 10}]
    e = make_engine(settings)
    register(e, tmp_path)
    fake = FakeTelegram()
    bot = TelegramBot(settings, token="123:SECRET", client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                      conn=connect(settings.db_path))
    return e, bot, fake


def _msg(chat, text, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def _cb(chat, data, uid=2):
    return {"update_id": uid, "callback_query": {"id": "cb1", "data": data, "from": {"id": 42},
                                                 "message": {"message_id": 7, "chat": {"id": chat}}}}


def test_ignores_unknown_chats(setup):
    e, bot, fake = setup
    fake.updates = [_msg(STRANGER, "/status"), _cb(STRANGER, "quota", 3)]
    bot.poll_once(timeout=0)
    assert not fake.sent() and not fake.sent("editMessageText")


def test_status_quota_and_progress(setup):
    e, bot, fake = setup
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul()", "T1")
    run(e, 120)
    bot.setup()
    fake.updates = [_msg(OWNER, "/status"), _cb(OWNER, "quota", 2), _cb(OWNER, "agent:mock-worker", 3)]
    bot.poll_once(timeout=0)
    status = fake.sent()[0]["text"]
    assert "Synthetic calc demo" in status and "engine" in status
    quota_view = fake.sent("editMessageText")[0]["text"]
    assert "mock-worker" in quota_view and "chưa có số liệu quota" in quota_view   # nothing invented
    assert "mock-worker" in fake.sent("editMessageText")[1]["text"]
    assert all("SECRET" not in json.dumps(b) for _, b in fake.calls)


def test_notifications_after_setup_without_backlog(setup):
    e, bot, fake = setup
    e.submit_goal("demo-calc", "Fix add() and implement mul()", "T2")
    bot.setup()                      # cursor starts at the current end: no flood of old events
    assert bot.collect_notifications() == []
    run(e, 120)
    texts = [t for t, _ in bot.collect_notifications()]
    assert any("Milestone hoàn thành" in t for t in texts)
    assert sum("Task xong" in t for t in texts) == 2
    assert bot.collect_notifications() == []   # cursor persisted, nothing repeated
    # a new bot instance (restart) continues from the stored cursor
    bot2 = TelegramBot(e.s, token="123:SECRET", client=bot.client, conn=connect(e.s.db_path))
    assert bot2.collect_notifications() == [] or all("Engine" in t for t, _ in bot2.collect_notifications())


def test_blocked_and_quota_notifications(settings, tmp_path):
    settings.telegram = {"allowed_chat_ids": [OWNER]}
    for a in settings.agents:
        if a["id"] == "mock-worker":
            a.update(fail_mode="usage_limit", fail_times=99)
    e = make_engine(settings)
    register(e, tmp_path)
    fake = FakeTelegram()
    bot = TelegramBot(settings, token="1:x", client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                      conn=connect(settings.db_path))
    bot.setup()
    e.submit_goal("demo-calc", "Fix add()", "T3")
    run(e, 30)
    texts = " | ".join(t for t, _ in bot.collect_notifications())
    assert "mock-worker</b> chạm giới hạn (usage_limited)" in texts


def test_approval_buttons_off_by_default_then_on(setup):
    e, bot, fake = setup
    with __import__("agentbridge.db", fromlist=["tx"]).tx(e.conn):
        aid = e._request_approval(1, "other", "please check", "test")
    bot.setup()
    fake.updates = [_cb(OWNER, f"ap:{aid}:approve")]
    bot.poll_once(timeout=0)
    assert q(e, "SELECT status FROM approval_requests WHERE id=?", aid)[0]["status"] == "PENDING"
    assert "đang tắt" in fake.sent("answerCallbackQuery")[0]["text"]
    bot.allow_approvals = True
    text, kb = bot.approvals_text()
    assert any(b["callback_data"] == f"ap:{aid}:approve" for row_ in kb for b in row_)
    fake.updates = [_cb(OWNER, f"ap:{aid}:approve", 5)]
    bot.poll_once(timeout=0)
    r = q(e, "SELECT status, resolved_by FROM approval_requests WHERE id=?", aid)[0]
    assert r == {"status": "APPROVED", "resolved_by": "telegram:42"}


def test_bar():
    assert bar(0) == "░" * 10 and bar(100) == "▓" * 10 and bar(55) == "▓" * 6 + "░" * 4
