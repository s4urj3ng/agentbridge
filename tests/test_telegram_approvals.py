"""Approval cards in Telegram: notification buttons, full text, edit after decision."""
from __future__ import annotations

from agentbridge.db import tx

from .conftest import q
from .test_telegram import OWNER, _cb, setup  # noqa: F401  (fixture)

LONG = "Start PORT-9B: port the capture pipeline\n" + "Chi tiết rất dài. " * 400


def _request(e, action="start_milestone", reason=LONG, evidence=None):
    pid = q(e, "SELECT id FROM projects")[0]["id"]
    with tx(e.conn):
        return e._request_approval(pid, action, reason, "test", evidence=evidence or {"goal": "PORT-9B goal text"})


def test_notification_carries_buttons_and_compact_card(setup):  # noqa: F811
    e, bot, fake = setup
    bot.setup()
    aid = _request(e)
    (text, kb), = [n for n in bot.collect_notifications() if "Cần bạn duyệt" in n[0]]
    assert "Bắt đầu milestone" in text and "demo-calc" in text and "Start PORT-9B" in text
    assert "Chi tiết" not in text and len(text) < 400                 # first line only
    assert [b["callback_data"] for r in kb for b in r] == [f"apf:{aid}"]   # approvals off: view only
    bot.allow_approvals = True
    aid2 = _request(e, "create_draft_pr", "x" * 500)
    (text, kb), = [n for n in bot.collect_notifications() if "Cần bạn duyệt" in n[0]]
    assert "Tạo draft PR" in text and "x" * 159 + "…" in text and "x" * 161 not in text
    assert [b["callback_data"] for r in kb for b in r] == [f"ap:{aid2}:approve:n", f"ap:{aid2}:reject:n", f"apf:{aid2}"]


def test_full_text_split_across_messages(setup):  # noqa: F811
    e, bot, fake = setup
    aid = _request(e)
    fake.updates = [_cb(OWNER, f"apf:{aid}")]
    bot.poll_once(timeout=0)
    msgs = [b["text"] for b in fake.sent()]
    assert len(msgs) >= 2 and all(len(m) <= 3900 for m in msgs) and "phần 1/" in msgs[0]
    joined = "".join(msgs)
    assert joined.count("Chi tiết rất dài.") >= 160 and "PORT-9B goal text" in joined   # reason + evidence
    assert not fake.sent("editMessageText")                                              # the card stays


def test_decision_edits_the_notification_and_second_tap_is_harmless(setup):  # noqa: F811
    e, bot, fake = setup
    bot.allow_approvals = True
    aid = _request(e, "unblock_task", "please unblock")
    fake.updates = [_cb(OWNER, f"ap:{aid}:approve:n")]
    bot.poll_once(timeout=0)
    assert q(e, "SELECT status, resolved_by FROM approval_requests WHERE id=?", aid)[0] == {
        "status": "APPROVED", "resolved_by": "telegram:42"}
    edit = fake.sent("editMessageText")[-1]
    assert edit["text"].startswith(f"✅ Đã duyệt #{aid} bởi bạn lúc ") and "Gỡ chặn task" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"] == []
    fake.calls.clear()
    fake.updates = [_cb(OWNER, f"ap:{aid}:reject:n", 9)]
    bot.poll_once(timeout=0)
    assert fake.sent("answerCallbackQuery")[0]["text"] == "Đã xử lý rồi."
    assert q(e, "SELECT status FROM approval_requests WHERE id=?", aid)[0]["status"] == "APPROVED"
    # reject from the list re-renders the list with compact cards
    aid2 = _request(e, "other", "decide this\nsecond line")
    text, kb = bot.approvals_text()
    assert "Cần quyết định" in text and "second line" not in text
    fake.updates = [_cb(OWNER, f"ap:{aid2}:reject", 10)]
    bot.poll_once(timeout=0)
    assert q(e, "SELECT status FROM approval_requests WHERE id=?", aid2)[0]["status"] == "REJECTED"
    assert "Không có gì chờ duyệt" in fake.sent("editMessageText")[-1]["text"]
