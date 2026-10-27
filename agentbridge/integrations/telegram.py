"""Telegram bot: quota and progress at a glance, plus push notifications.

Outbound only: long polling (getUpdates), no webhook and no open port.
Security:
  * replies only to chat ids in `telegram.allowed_chat_ids` (or
    TELEGRAM_ALLOWED_CHAT_IDS); other chats are ignored and their id is logged
    so the owner can find their own id during setup;
  * the bot token comes from TELEGRAM_BOT_TOKEN (agentbridge.env) and is never logged;
  * approve/reject buttons exist only when `telegram.allow_approvals: true`
    (default off), and still go through Engine.resolve_approval policy checks.
Everything shown is redacted and summarised; no logs, diffs or prompts are sent.
"""
from __future__ import annotations

import html
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import Settings
from ..db import audit, connect, jload, row, rows, tx
from ..policies.safety import PolicyViolation, redact
from ..reporting import engine_running, overview, quota_report

log = logging.getLogger("agentbridge.telegram")

DEFAULT_NOTIFY = ["milestone_completed", "task_blocked", "task_completed", "approval_requested",
                  "agent_cooldown", "agent_auth", "agent_circuit", "fallback", "engine_down", "engine_error",
                  "draft_pr"]
MAX_LEN = 3900

MAIN_MENU = [[{"text": "📊 Quota", "callback_data": "quota"}, {"text": "📁 Dự án", "callback_data": "overview"}],
             [{"text": "📝 Chờ duyệt", "callback_data": "approvals"}, {"text": "🔄 Làm mới", "callback_data": "refresh"}]]


def _stop_text(reason: str) -> str:
    """'provider window seven_day at 95% (stop at 85%) until …' → 'tuần đã dùng 95% (ngưỡng 85%)'."""
    import re
    m = re.search(r"provider window (\w+) (?:at ([\d.]+)% \(stop at ([\d.]+)%\)|exhausted)", reason or "")
    if not m:
        return reason or ""
    name = {"five_hour": "5h", "seven_day": "tuần"}.get(m.group(1), m.group(1))
    return f"{name} đã hết" if m.group(2) is None else f"{name} đã dùng {m.group(2)}% (ngưỡng {m.group(3)}%)"


def bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, round(pct * width / 100)))
    return "▓" * filled + "░" * (width - filled)


def esc(s: Any) -> str:
    return html.escape(redact(str(s if s is not None else "")))


class TelegramBot:
    def __init__(self, settings: Settings, *, token: str | None = None, client: httpx.Client | None = None,
                 conn: sqlite3.Connection | None = None):
        self.s = settings
        cfg = settings.telegram or {}
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (put it in /etc/agentbridge/agentbridge.env)")
        ids = cfg.get("allowed_chat_ids") or [x for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x]
        self.allowed = {int(x) for x in ids}
        self.allow_approvals = bool(cfg.get("allow_approvals", False))
        self.notify_kinds = set(cfg.get("notify") or DEFAULT_NOTIFY)
        self.tz = ZoneInfo(cfg.get("timezone", "Asia/Ho_Chi_Minh"))
        self.client = client or httpx.Client(timeout=40)
        self.conn = conn or connect(settings.db_path)
        self.offset = 0
        self._engine_up: bool | None = None

    # ------------------------------------------------------------ Telegram API
    def api(self, method: str, **params) -> Any:
        r = self.client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params)
        d = r.json()
        if not d.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {d.get('description', r.status_code)}")
        return d.get("result")

    def send(self, chat_id: int, text: str, keyboard: list | None = None) -> None:
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN] + "\n…"
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                  "disable_web_page_preview": True}
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        self.api("sendMessage", **params)

    def broadcast(self, text: str, keyboard: list | None = None) -> None:
        for chat in self.allowed:
            try:
                self.send(chat, text, keyboard)
            except Exception as e:  # one failing chat must not stop the others
                log.warning("send to %s failed: %s", chat, redact(str(e)))

    # ------------------------------------------------------------ time
    def t(self, iso: str | None) -> str:
        if not iso:
            return "?"
        return datetime.fromisoformat(iso).astimezone(self.tz).strftime("%H:%M %d/%m")

    # ------------------------------------------------------------ views
    def refresh_quota(self, max_age_s: float = 60) -> None:
        """Free probes (Antigravity /usage, 0 tokens) when the last one is older than max_age_s."""
        from ..quota_probe import probe_age_s, refresh_free_quotas
        age = probe_age_s(self.conn)
        if age is not None and age <= max_age_s:
            return
        # time-boxed: if the provider command is slow, show the last stored numbers instead of freezing
        import threading
        def work():
            try:
                refresh_free_quotas(self.s)   # own DB connection: safe in a thread
            except Exception as e:
                log.warning("quota probe failed: %s", redact(str(e)))
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout=25)

    def quota_text(self) -> tuple[str, list]:
        self.refresh_quota()
        lines = ["<b>📊 Quota</b>", "<i>✅ số thật nhà cung cấp báo · 📐 ngân sách bạn đặt · "
                                   "🟡 vượt ngưỡng, việc chuyển sang agent dự phòng</i>", ""]
        buttons = []
        for a in quota_report(self.conn):
            icon = {"ok": "🟢", "cooldown": "🔴", "circuit_open": "🟠", "paused": "⏸", "provider_stop": "🟡"}[a["state"]]
            head = f"{icon} <b>{esc(a['id'])}</b> · {esc(a['model'] or a['provider'])}"
            if a["running"]:
                head += f" · đang chạy {a['running']}/{a['max_concurrency']}"
            lines.append(head)
            if a["state"] == "cooldown":
                lines.append(f"   nghỉ đến {self.t(a['until'])} ({esc(a['reason'])})")
            elif a["state"] == "circuit_open":
                lines.append(f"   lỗi liên tiếp, tạm ngắt đến {self.t(a['until'])}")
            elif a["state"] == "provider_stop":
                fb = a.get("fallback_to") or []
                lines.append(f"   đang nhường việc: {esc(_stop_text(a['reason']))}"
                             + (f" → {esc(', '.join(fb))}" if fb else ""))
            elif a["state"] == "paused":
                lines.append(f"   tạm dừng: {esc(a['reason'] or 'bởi chủ')}")
            for w in a["provider_windows"]:
                if w["stale"]:
                    lines.append(f"   ✅ {w['label']:>3} đã reset lúc {self.t(w['resets_at'])} (chưa đo lại)")
                elif w["left_pct"] is not None:
                    lines.append(f"   ✅ {w['label']:>3} {bar(w['left_pct'])} còn {w['left_pct']}%"
                                 + (f" · reset {self.t(w['resets_at'])}" if w["resets_at"] else ""))
                else:
                    lines.append(f"   ✅ {w['label']:>3} {w['status']} · reset {self.t(w['resets_at'])}")
            if a["provider_windows"]:
                lines.append(f"      <i>đo lúc {self.t(a['provider_quota_at'])}</i>")
            for w in a["windows"]:
                lines.append(f"   📐 {w['label']:>3} {bar(w['left_pct'])} {w['left_pct']}% ({w['used']}/{w['max']})")
            if not a["windows"] and not a["provider_windows"]:
                lines.append(f"   không có số liệu quota · 7 ngày: {a['calls_7d']} lượt")
            buttons.append({"text": a["id"], "callback_data": f"agent:{a['id']}"[:64]})
        kb = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [[{"text": "⬅️ Menu", "callback_data": "menu"}]]
        return "\n".join(lines), kb

    def agent_text(self, agent_id: str) -> tuple[str, list]:
        a = next((x for x in quota_report(self.conn) if x["id"] == agent_id), None)
        if a is None:
            return "Không tìm thấy agent.", [[{"text": "⬅️ Quota", "callback_data": "quota"}]]
        lines = [f"<b>{esc(a['id'])}</b> ({esc(a['provider'])}, {esc(a['model'] or '-')})",
                 f"Trạng thái: {a['state']}" + (f" đến {self.t(a['until'])}" if a["until"] else ""),
                 f"7 ngày: {a['calls_7d']} lượt, {a['tokens_7d']:,} token (khi CLI có báo)"]
        for w in a["provider_windows"]:
            lines.append(f"✅ {a['provider_source']}: {w['label']} đã dùng {w['used_pct']}% "
                         f"({w['status']}), reset {self.t(w['resets_at'])}" + (" — đã qua, chưa đo lại" if w["stale"] else ""))
        for w in a["windows"]:
            lines.append(f"📐 ngân sách {w['label']}: dùng {w['used']}/{w['max']} → còn {w['left_pct']}%")
        if a["last_limit_error"]:
            lines.append(f"Lần cuối nhà cung cấp báo giới hạn: {self.t(a['last_limit_error']['started_at'])} "
                         f"({a['last_limit_error']['outcome']})")
        return "\n".join(lines), [[{"text": "⬅️ Quota", "callback_data": "quota"}]]

    def overview_text(self) -> tuple[str, list]:
        ov = overview(self.conn)
        up = engine_running(self.s.data_dir)
        lines = [f"<b>📁 Tổng quan</b> · engine {'🟢 chạy' if up else '🔴 dừng'}",
                 f"Hàng đợi: {ov['queued']} · chờ duyệt: {ov['pending_approvals']}", ""]
        for p in ov["projects"]:
            lines.append(f"<b>{esc(p['name'])}</b>" + (" ⏸" if p["paused"] else ""))
            if not p["milestones"]:
                last = p["last_completed"]
                lines.append("   không có milestone đang chạy" +
                             (f" · xong gần nhất: {esc(last['key'])} ({self.t(last['completed_at'])})" if last else ""))
            for m in p["milestones"]:
                lines.append(f"   {esc(m['key'])} [{m['status']}] {bar(m['pct'])} {m['pct']}% ({m['done']}/{m['total']})")
                lines.append(f"   <i>{esc(m['goal'][:120])}</i>")
                for t in m["running"]:
                    lines.append(f"   ▶️ {esc(t['key'])} · {esc(t['assignee'])} ({t['state']})")
                for t in m["review"]:
                    lines.append(f"   🔍 {esc(t['key'])} chờ review" + (f" · {esc(t['reviewer'])}" if t["reviewer"] else ""))
                for t in m["blocked"]:
                    lines.append(f"   ⛔ {esc(t['key'])}: {esc((t['blocked_reason'] or '')[:150])}")
            lines.append("")
        return "\n".join(lines), MAIN_MENU

    def approvals_text(self) -> tuple[str, list]:
        aps = rows(self.conn, "SELECT a.*, p.slug FROM approval_requests a JOIN projects p ON p.id=a.project_id "
                              "WHERE a.status='PENDING' ORDER BY a.id LIMIT 10")
        if not aps:
            return "Không có gì chờ duyệt. ✅", MAIN_MENU
        lines, kb = ["<b>📝 Chờ duyệt</b>"], []
        for a in aps:
            lines.append(f"\n#{a['id']} <b>{esc(a['action'])}</b> · {esc(a['slug'])} · {self.t(a['created_at'])}\n"
                         f"{esc(a['reason'][:400])}")
            if self.allow_approvals:
                kb.append([{"text": f"✅ Duyệt #{a['id']}", "callback_data": f"ap:{a['id']}:approve"},
                           {"text": f"❌ Từ chối #{a['id']}", "callback_data": f"ap:{a['id']}:reject"}])
        if not self.allow_approvals:
            lines.append("\n<i>Duyệt bằng dashboard hoặc `agentbridge approvals approve ID` "
                         "(nút duyệt trên Telegram đang tắt).</i>")
        return "\n".join(lines), kb + [[{"text": "⬅️ Menu", "callback_data": "menu"}]]

    # ------------------------------------------------------------ updates
    def handle_update(self, u: dict) -> None:
        msg = u.get("message")
        cq = u.get("callback_query")
        chat_id = (msg or {}).get("chat", {}).get("id") or ((cq or {}).get("message") or {}).get("chat", {}).get("id")
        if chat_id is None:
            return
        if chat_id not in self.allowed:
            log.warning("ignored update from chat %s (not in allowed_chat_ids)", chat_id)
            return
        if msg:
            text = (msg.get("text") or "").strip().split("@")[0]
            view = {"/quota": self.quota_text, "/status": self.overview_text, "/approvals": self.approvals_text}.get(text)
            if view:
                self.send(chat_id, *view())
            else:
                self.send(chat_id, "AgentBridge 👋 Chọn mục:", MAIN_MENU)
            return
        data = cq.get("data", "")
        user = cq.get("from", {}).get("id")
        try:
            if data.startswith("ap:"):
                reply = self._approval_action(data, user)
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text=reply[:190])
                text, kb = self.approvals_text()
            else:
                self.api("answerCallbackQuery", callback_query_id=cq["id"])
                if data.startswith("agent:"):
                    text, kb = self.agent_text(data[6:])
                else:
                    text, kb = {"quota": self.quota_text, "overview": self.overview_text, "refresh": self.overview_text,
                                "approvals": self.approvals_text}.get(data, lambda: ("Chọn mục:", MAIN_MENU))()
            self.api("editMessageText", chat_id=chat_id, message_id=cq["message"]["message_id"], text=text[:MAX_LEN],
                     parse_mode="HTML", disable_web_page_preview=True, reply_markup={"inline_keyboard": kb})
        except RuntimeError as e:
            if "message is not modified" not in str(e):
                raise

    def _approval_action(self, data: str, user_id: int | None) -> str:
        if not self.allow_approvals:
            return "Duyệt qua Telegram đang tắt."
        _, aid, action = data.split(":")
        from ..orchestrator.engine import Engine
        try:
            Engine(self.s, self.conn).resolve_approval(int(aid), action == "approve", f"telegram:{user_id}",
                                                       "via Telegram")
        except (ValueError, PolicyViolation) as e:
            return f"Không thực hiện được: {e}"
        return f"Đã {'duyệt' if action == 'approve' else 'từ chối'} #{aid}"

    # ------------------------------------------------------------ notifications
    def _cursor(self, key: str, table: str) -> int:
        r = row(self.conn, "SELECT value FROM settings WHERE key=?", key)
        if r:
            return int(r["value"])
        start = row(self.conn, f"SELECT COALESCE(MAX(id), 0) m FROM {table}")["m"]  # no backlog flood on first start
        self._set_cursor(key, start)
        return start

    def _set_cursor(self, key: str, value: int) -> None:
        with tx(self.conn):
            self.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, str(value)))

    def collect_notifications(self) -> list[tuple[str, list | None]]:
        out: list[tuple[str, list | None]] = []
        cur = self._cursor("telegram.audit_cursor", "audit_log")
        last = cur
        for e in rows(self.conn, "SELECT * FROM audit_log WHERE id > ? ORDER BY id LIMIT 200", cur):
            last = e["id"]
            msg = self._format_event(e)
            if msg:
                out.append((msg, None))
        if last != cur:
            self._set_cursor("telegram.audit_cursor", last)
        cur = self._cursor("telegram.approval_cursor", "approval_requests")
        last = cur
        for a in rows(self.conn, "SELECT a.*, p.slug FROM approval_requests a JOIN projects p ON p.id=a.project_id "
                                 "WHERE a.id > ? ORDER BY a.id", cur):
            last = a["id"]
            if "approval_requested" in self.notify_kinds and a["status"] == "PENDING":
                kb = [[{"text": "📝 Xem chờ duyệt", "callback_data": "approvals"}]]
                out.append((f"📝 <b>Cần bạn duyệt</b> #{a['id']} ({esc(a['action'])}) · {esc(a['slug'])}\n"
                            f"{esc(a['reason'][:500])}", kb))
        if last != cur:
            self._set_cursor("telegram.approval_cursor", last)
        up = engine_running(self.s.data_dir)
        if self._engine_up is not None and up != self._engine_up and "engine_down" in self.notify_kinds:
            out.append(("🟢 Engine đã chạy lại." if up else "🔴 <b>Engine đã dừng</b> (systemd sẽ tự khởi động lại; "
                         "nếu không, kiểm tra server).", None))
        self._engine_up = up
        return out

    def _task_key(self, task_id: int | None) -> str:
        t = row(self.conn, "SELECT t.key, p.slug FROM tasks t JOIN projects p ON p.id=t.project_id WHERE t.id=?",
                task_id) if task_id else None
        return f"{t['slug']}/{t['key']}" if t else f"task {task_id}"

    def _format_event(self, e: sqlite3.Row) -> str | None:
        d = jload(e["detail_json"], {})
        k, act = self.notify_kinds, e["action"]
        if act == "task.transition":
            if d.get("dst") == "BLOCKED" and "task_blocked" in k:
                return f"⛔ <b>Task bị chặn</b>: {esc(self._task_key(e['task_id']))}\n{esc(d.get('reason', '')[:300])}"
            if d.get("dst") == "COMPLETED" and "task_completed" in k:
                return f"✅ Task xong: {esc(self._task_key(e['task_id']))}"
            return None
        if act == "milestone.complete" and "milestone_completed" in k:
            return f"🏁 <b>Milestone hoàn thành</b>: {esc(d.get('milestone'))}"
        if act == "agent.cooldown" and "agent_cooldown" in k:
            return (f"⏳ <b>{esc(d.get('agent'))}</b> chạm giới hạn ({esc(d.get('outcome'))}), "
                    f"nghỉ ~{round(int(d.get('seconds', 0)) / 60)} phút. Việc sẽ chuyển sang agent dự phòng nếu có.")
        if act == "agent.paused_auth" and "agent_auth" in k:
            return f"🔑 <b>{esc(d.get('agent'))}</b> mất đăng nhập — cần đăng nhập lại trên server rồi `agents resume`."
        if act == "agent.circuit_open" and "agent_circuit" in k:
            return f"⚠️ <b>{esc(d.get('agent'))}</b> lỗi {esc(d.get('failures'))} lần liên tiếp, tạm ngắt 10 phút."
        if act in ("task.fallback", "review.fallback") and "fallback" in k:
            what = "Task" if act == "task.fallback" else "Review"
            return f"↪️ {what} {esc(self._task_key(e['task_id']))}: {esc(d.get('src'))} → {esc(d.get('dst'))}"
        if act == "job.error" and "engine_error" in k:
            return f"❗ Lỗi nội bộ engine ({esc(d.get('job'))}). Xem `agentbridge events`."
        if act == "recovery" and "engine_error" in k:
            return f"♻️ Engine khởi động lại và khôi phục {len(d.get('notes', []))} mục."
        if act == "github.draft_pr" and "draft_pr" in k:
            return f"🔗 Draft PR: {esc(d.get('url'))}"
        return None

    # ------------------------------------------------------------ loop
    def setup(self) -> None:
        self.api("setMyCommands", commands=[{"command": "status", "description": "Tổng quan dự án"},
                                            {"command": "quota", "description": "Quota các agent"},
                                            {"command": "approvals", "description": "Việc chờ duyệt"}])
        self._cursor("telegram.audit_cursor", "audit_log")
        self._cursor("telegram.approval_cursor", "approval_requests")
        self._engine_up = engine_running(self.s.data_dir)
        with tx(self.conn):
            audit(self.conn, "telegram", "telegram.start", chats=len(self.allowed), approvals=self.allow_approvals)

    def poll_once(self, timeout: int = 25) -> None:
        updates = self.api("getUpdates", offset=self.offset, timeout=timeout,
                           allowed_updates=["message", "callback_query"])
        for u in updates or []:
            self.offset = u["update_id"] + 1
            try:
                self.handle_update(u)
            except Exception as e:
                log.warning("update handling failed: %s", redact(str(e)))
        for text, kb in self.collect_notifications():
            self.broadcast(text, kb)

    def run_forever(self) -> None:
        if not self.allowed:
            raise RuntimeError("no allowed chat ids: set telegram.allowed_chat_ids (use `agentbridge telegram --discover`)")
        self.setup()
        backoff = 5
        while True:
            try:
                self.poll_once()
                backoff = 5
            except (httpx.HTTPError, RuntimeError) as e:
                log.warning("telegram poll failed: %s", redact(str(e)))
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def discover(self) -> list[dict]:
        """Chat ids that recently messaged the bot (setup helper; does not reply)."""
        seen = {}
        for u in self.api("getUpdates", timeout=0) or []:
            c = (u.get("message") or {}).get("chat") or ((u.get("callback_query") or {}).get("message") or {}).get("chat")
            if c:
                seen[c["id"]] = {"chat_id": c["id"], "type": c.get("type"),
                                 "name": c.get("username") or c.get("title") or c.get("first_name")}
        return list(seen.values())
