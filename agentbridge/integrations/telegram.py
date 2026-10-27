"""Telegram bot: quota and progress at a glance, plus push notifications.

Outbound only: long polling (getUpdates), no webhook and no open port.
Security:
  * replies only to chat ids in `telegram.allowed_chat_ids` (or
    TELEGRAM_ALLOWED_CHAT_IDS); other chats are ignored and their id is logged
    so the owner can find their own id during setup;
  * the bot token comes from TELEGRAM_BOT_TOKEN (agentbridge.env) and is never logged;
  * approve/reject buttons exist only when `telegram.allow_approvals: true`
    (default off), and still go through Engine.resolve_approval policy checks;
  * role assignment / routing order ("⚙️ Phân công") is read-only unless
    `telegram.allow_config_changes: true` (default off); every change needs an inline
    confirm, is validated (orchestrator/overrides.py), audited as telegram:<user id> and
    announced to all allowed chats.
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
from ..orchestrator import overrides
from ..policies.safety import PolicyViolation, redact
from ..reporting import engine_running, overview, quota_report

log = logging.getLogger("agentbridge.telegram")

DEFAULT_NOTIFY = ["milestone_completed", "task_blocked", "task_completed", "approval_requested",
                  "agent_cooldown", "agent_auth", "agent_circuit", "fallback", "engine_down", "engine_error",
                  "draft_pr", "config_change"]
MAX_LEN = 3900

MAIN_MENU = [[{"text": "📊 Quota", "callback_data": "quota"}, {"text": "📁 Dự án", "callback_data": "overview"}],
             [{"text": "📝 Chờ duyệt", "callback_data": "approvals"}, {"text": "⚙️ Phân công", "callback_data": "cfg"}],
             [{"text": "🔄 Làm mới", "callback_data": "refresh"}]]

# Vietnamese labels for the runtime override keys
KEY_LABELS = {"default_worker": "worker mặc định", "workers": "thứ tự worker", "reviewers": "thứ tự reviewer",
              "fallback_to": "thứ tự dự phòng", "enabled": "bật/tắt", "max_concurrency": "số việc song song"}
PROJECT_FIELDS = {"dw": "default_worker", "wk": "workers", "rv": "reviewers"}
AGENT_FIELDS = {"fb": "fallback_to", "mc": "max_concurrency", "en": "enabled"}
CFG_OFF = "Thay đổi phân công qua Telegram đang tắt."


def _stop_text(reason: str) -> str:
    """'provider window seven_day at 95% (stop at 85%) until …' → 'tuần đã dùng 95%'."""
    import re
    m = re.search(r"provider window (\w+) (?:at ([\d.]+)% \(stop at ([\d.]+)%\)|exhausted)", reason or "")
    if not m:
        return reason or ""
    name = {"five_hour": "5h", "seven_day": "tuần"}.get(m.group(1), m.group(1))
    return f"{name} đã hết" if m.group(2) is None else f"{name} đã dùng {m.group(2)}%"


def bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, round(pct * width / 100)))
    return "▓" * filled + "░" * (width - filled)


def _fmt(v: Any) -> str:
    """Override value for humans: lists as 'a → b', None as 'cấu hình gốc'."""
    if v is None:
        return "cấu hình gốc"
    if isinstance(v, bool):
        return "bật" if v else "tắt"
    if isinstance(v, list):
        return " → ".join(map(str, v)) or "(trống)"
    return str(v)


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
        self.allow_config_changes = bool(cfg.get("allow_config_changes", False))
        self.drafts: dict[int, dict] = {}    # chat id -> pending (unconfirmed) assignment change
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
        lines = ["<b>📊 Quota</b>", "<i>✅ số thật nhà cung cấp báo · 🟡 đang nhường việc cho agent dự phòng</i>", ""]
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
            if not a["provider_windows"]:
                lines.append(f"   chưa có số liệu quota · 7 ngày: {a['calls_7d']} lượt")
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

    # ------------------------------------------------------------ assignment (⚙️ Phân công)
    # callback_data uses list indices (cfg:p:<i>, cfg:a:<i>, cfg:d:<option i>) so it stays far below
    # 64 bytes whatever the slugs and agent ids are; the pending change lives in self.drafts.
    def _projects(self) -> list[str]:
        return [r["slug"] for r in rows(self.conn, "SELECT slug FROM projects ORDER BY id")]

    def _agent_rows(self) -> list[sqlite3.Row]:
        agents = rows(self.conn, "SELECT id, provider, roles_json, enabled, max_concurrency, config_json "
                                 "FROM agents ORDER BY id")
        if any(a["provider"] != "mock" for a in agents):   # same rule as the quota view: hide demo identities
            agents = [a for a in agents if a["provider"] != "mock"]
        return agents

    def assign_text(self, note: str = "") -> tuple[str, list]:
        lines = ["<b>⚙️ Phân công</b>", "<i>✏️ = đang ghi đè cấu hình gốc (lưu trong DB)</i>", ""]
        kb = []
        for i, slug in enumerate(self._projects()):
            p = overrides.effective_project(self.conn, slug)
            mark = " ✏️" if p["overridden"] else ""
            lines.append(f"📁 <b>{esc(slug)}</b>{mark} · lead {esc(p['tech_lead'])}")
            lines.append(f"   👷 {esc(_fmt(list(dict.fromkeys([p['default_worker'], *p['workers']]))))}")
            lines.append(f"   🔍 {esc(_fmt(p['reviewers']))}")
            kb.append([{"text": f"📁 {slug}", "callback_data": f"cfg:p:{i}"}])
        agent_ov = overrides.all_agent_overrides(self.conn)
        buttons = []
        for i, a in enumerate(self._agent_rows()):
            mark = " ✏️" if a["id"] in agent_ov else ""
            buttons.append({"text": ("" if a["enabled"] else "⏸ ") + a["id"] + mark, "callback_data": f"cfg:a:{i}"})
        kb += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        if not self.allow_config_changes:
            lines.append("\n<i>Chỉ xem. Bật `telegram.allow_config_changes: true` để đổi phân công từ Telegram.</i>")
        if note:
            lines.insert(1, note)
        return "\n".join(lines), kb + [[{"text": "⬅️ Menu", "callback_data": "menu"}]]

    def project_cfg_text(self, i: int) -> tuple[str, list]:
        slug = self._projects()[i]
        p = overrides.effective_project(self.conn, slug)
        ov = set(p["overridden"])
        m = lambda k: " ✏️" if k in ov else ""
        lines = [f"<b>📁 {esc(slug)}</b> · Tech Lead {esc(p['tech_lead'])}",
                 f"👷 Worker mặc định{m('default_worker')}: <b>{esc(p['default_worker'])}</b>",
                 f"📋 Thứ tự worker{m('workers')}: {esc(_fmt(p['workers']))}",
                 f"🔍 Thứ tự reviewer{m('reviewers')} (đầu tiên = ưu tiên): {esc(_fmt(p['reviewers']))}",
                 "<i>Reviewer không bao giờ review task do chính mình làm.</i>"]
        kb = []
        if self.allow_config_changes:
            kb += [[{"text": "👷 Worker mặc định", "callback_data": f"cfg:pe:{i}:dw"}],
                   [{"text": "📋 Thứ tự worker", "callback_data": f"cfg:pe:{i}:wk"},
                    {"text": "🔍 Thứ tự reviewer", "callback_data": f"cfg:pe:{i}:rv"}]]
            if ov:
                kb.append([{"text": "♻️ Về cấu hình gốc", "callback_data": f"cfg:pr:{i}"}])
        return "\n".join(lines), kb + [[{"text": "⬅️ Phân công", "callback_data": "cfg"}]]

    def agent_cfg_text(self, i: int) -> tuple[str, list]:
        a = self._agent_rows()[i]
        ov = overrides.get(self.conn, "agent", a["id"])
        m = lambda k: " ✏️" if k in ov else ""
        fb = overrides.fallback_chain(self.conn, a["id"], jload(a["config_json"], {}))
        lines = [f"<b>{esc(a['id'])}</b> ({esc(a['provider'])}) · vai trò: {esc(', '.join(jload(a['roles_json'], [])))}",
                 f"⏯ Trạng thái{m('enabled')}: {'🟢 bật' if a['enabled'] else '⏸ tắt'}",
                 f"🔢 Số việc song song{m('max_concurrency')}: {a['max_concurrency']}",
                 f"↪️ Thứ tự dự phòng{m('fallback_to')}: {esc(_fmt(fb))}"]
        kb = []
        if self.allow_config_changes:
            kb += [[{"text": "↪️ Thứ tự dự phòng", "callback_data": f"cfg:ae:{i}:fb"},
                    {"text": "🔢 Song song", "callback_data": f"cfg:ae:{i}:mc"}],
                   [{"text": "⏸ Tắt agent" if a["enabled"] else "▶️ Bật agent", "callback_data": f"cfg:ae:{i}:en"}]]
            if ov:
                kb.append([{"text": "♻️ Về cấu hình gốc", "callback_data": f"cfg:ar:{i}"}])
        return "\n".join(lines), kb + [[{"text": "⬅️ Phân công", "callback_data": "cfg"}]]

    def _draft_back(self, d: dict) -> tuple[str, list]:
        try:
            if d["scope"] == "project":
                return self.project_cfg_text(self._projects().index(d["target"]))
            return self.agent_cfg_text([a["id"] for a in self._agent_rows()].index(d["target"]))
        except ValueError:
            return self.assign_text()

    def _draft_text(self, d: dict) -> tuple[str, list]:
        """Ordered pick list: tap agents in order of preference, then ✅ Xong."""
        lines = [f"<b>{esc(d['target'])}</b> · {KEY_LABELS[d['key']]}",
                 "Chạm lần lượt theo thứ tự ưu tiên (đầu tiên = ưu tiên nhất).", "",
                 "Đã chọn: " + (esc(_fmt(d["value"])) if d["value"] else "<i>(chưa có)</i>")]
        opts = [{"text": f"➕ {o}", "callback_data": f"cfg:d:{k}"} for k, o in enumerate(d["options"])
                if o not in d["value"]]
        kb = [opts[i:i + 2] for i in range(0, len(opts), 2)]
        kb.append([{"text": "🧹 Chọn lại", "callback_data": "cfg:d:clr"}, {"text": "✅ Xong", "callback_data": "cfg:d:ok"}])
        return "\n".join(lines), kb + [[{"text": "✖️ Huỷ", "callback_data": "cfg:n"}]]

    def _choice_text(self, d: dict) -> tuple[str, list]:
        """Single choice (worker mặc định, số việc song song): tapping one goes to the confirm step."""
        lines = [f"<b>{esc(d['target'])}</b> · {KEY_LABELS[d['key']]}", f"Hiện tại: <b>{esc(_fmt(d['old']))}</b>",
                 "Chọn giá trị mới:"]
        opts = [{"text": str(o), "callback_data": f"cfg:d:{k}"} for k, o in enumerate(d["options"])]
        width = 4 if d["key"] == "max_concurrency" else 2
        kb = [opts[i:i + width] for i in range(0, len(opts), width)]
        return "\n".join(lines), kb + [[{"text": "✖️ Huỷ", "callback_data": "cfg:n"}]]

    def _confirm_text(self, d: dict) -> tuple[str, list]:
        if d.get("reset"):
            what = f"Đưa <b>{esc(d['target'])}</b> về cấu hình gốc (bỏ {esc(', '.join(KEY_LABELS[k] for k in d['keys']))})?"
        else:
            what = (f"<b>{esc(d['target'])}</b> · {KEY_LABELS[d['key']]}\n"
                    f"{esc(_fmt(d['old']))} → <b>{esc(_fmt(d['value']))}</b>")
        text = ("<b>Xác nhận thay đổi?</b>\n" + what +
                "\n\n<i>Áp dụng cho các lần giao việc tiếp theo; việc đang chạy không bị dừng. "
                "Thay đổi được ghi audit và báo cho mọi chat được phép.</i>")
        return text, [[{"text": "✅ Xác nhận", "callback_data": "cfg:y"}, {"text": "✖️ Huỷ", "callback_data": "cfg:n"}]]

    def _start_draft(self, chat_id: int, scope: str, idx: int, field: str) -> tuple[str, tuple[str, list]]:
        if scope == "project":
            slug, key = self._projects()[idx], PROJECT_FIELDS[field]
            p = overrides.effective_project(self.conn, slug)
            role = "reviewer" if key == "reviewers" else "worker"
            d = {"scope": "project", "target": slug, "key": key, "old": p[key],
                 "options": overrides.eligible(self.conn, role), "value": [] if key != "default_worker" else None}
        else:
            a = self._agent_rows()[idx]
            key = AGENT_FIELDS[field]
            if key == "enabled":
                d = {"scope": "agent", "target": a["id"], "key": key, "old": bool(a["enabled"]),
                     "value": not a["enabled"], "options": []}
                self.drafts[chat_id] = d
                return "", self._confirm_text(d)
            if key == "max_concurrency":
                d = {"scope": "agent", "target": a["id"], "key": key, "old": a["max_concurrency"],
                     "options": list(range(overrides.MAX_CONCURRENCY + 1)), "value": None}
            else:
                roles = set(jload(a["roles_json"], []))
                opts = [x["id"] for x in rows(self.conn, "SELECT id, roles_json FROM agents ORDER BY id")
                        if x["id"] != a["id"] and roles & set(jload(x["roles_json"], []))]
                d = {"scope": "agent", "target": a["id"], "key": key, "value": [], "options": opts,
                     "old": overrides.fallback_chain(self.conn, a["id"], jload(a["config_json"], {}))}
        self.drafts[chat_id] = d
        return "", (self._draft_text(d) if isinstance(d["value"], list) else self._choice_text(d))

    def _config_action(self, data: str, chat_id: int, user_id: int | None) -> tuple[str, tuple[str, list]]:
        """(callback answer, new view) for every cfg… button."""
        parts = data.split(":")
        try:
            if data == "cfg":
                return "", self.assign_text()
            if parts[1] == "p":
                return "", self.project_cfg_text(int(parts[2]))
            if parts[1] == "a":
                return "", self.agent_cfg_text(int(parts[2]))
            # everything below changes standing configuration
            if not self.allow_config_changes:
                self.drafts.pop(chat_id, None)
                return CFG_OFF, self.assign_text()
            if parts[1] in ("pe", "ae"):
                return self._start_draft(chat_id, "project" if parts[1] == "pe" else "agent", int(parts[2]), parts[3])
            if parts[1] in ("pr", "ar"):
                scope = "project" if parts[1] == "pr" else "agent"
                target = self._projects()[int(parts[2])] if scope == "project" else self._agent_rows()[int(parts[2])]["id"]
                d = {"scope": scope, "target": target, "reset": True,
                     "keys": sorted(overrides.get(self.conn, scope, target))}
                self.drafts[chat_id] = d
                return "", self._confirm_text(d)
            d = self.drafts.get(chat_id)
            if d is None:
                return "Phiên chỉnh đã hết, mở lại.", self.assign_text()
            if parts[1] == "n":
                self.drafts.pop(chat_id, None)
                return "Đã huỷ.", self._draft_back(d)
            if parts[1] == "d" and not d.get("reset"):
                if parts[2] == "clr":
                    d["value"] = []
                    return "", self._draft_text(d)
                if parts[2] == "ok":
                    return "", self._confirm_text(d)
                choice = d["options"][int(parts[2])]
                if isinstance(d["value"], list):
                    if choice not in d["value"]:
                        d["value"].append(choice)
                    return "", self._draft_text(d)
                d["value"] = choice
                return "", self._confirm_text(d)
            if parts[1] == "y":
                self.drafts.pop(chat_id, None)
                actor = f"telegram:{user_id}"
                if d.get("reset"):
                    overrides.reset(self.conn, d["scope"], d["target"], actor, self.s.agents)
                    return "Đã về cấu hình gốc.", self._draft_back(d)
                try:
                    overrides.set_override(self.conn, d["scope"], d["target"], d["key"], d["value"], actor)
                except ValueError as e:
                    return f"Không lưu: {e}", self._draft_back(d)
                return "Đã lưu.", self._draft_back(d)
        except (IndexError, KeyError, ValueError):   # stale or forged button
            self.drafts.pop(chat_id, None)
            return "Nút không còn hợp lệ, mở lại.", self.assign_text()
        return "", self.assign_text()

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
            view = {"/quota": self.quota_text, "/status": self.overview_text, "/approvals": self.approvals_text,
                    "/assign": self.assign_text}.get(text)
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
            elif data == "cfg" or data.startswith("cfg:"):
                reply, (text, kb) = self._config_action(data, chat_id, user)
                self.api("answerCallbackQuery", callback_query_id=cq["id"], **({"text": reply[:190]} if reply else {}))
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
        if act in ("task.reroute", "review.reroute") and "config_change" in k:
            what = "Task" if act == "task.reroute" else "Review"
            return (f"↪️ {what} {esc(self._task_key(e['task_id']))} theo phân công mới: "
                    f"{esc(d.get('src'))} → {esc(d.get('dst'))}")
        if act == "config.override" and "config_change" in k:
            return (f"⚙️ <b>Phân công thay đổi</b> ({esc(e['actor'])}): {esc(d.get('target'))} · "
                    f"{KEY_LABELS.get(d.get('key'), esc(d.get('key')))}: {esc(_fmt(d.get('old')))} → "
                    f"<b>{esc(_fmt(d.get('value')))}</b>")
        if act == "config.reset" and "config_change" in k:
            return (f"♻️ <b>Phân công về cấu hình gốc</b> ({esc(e['actor'])}): {esc(d.get('target'))}"
                    + (f" · {', '.join(KEY_LABELS.get(x, x) for x in d.get('keys') or [])}" if d.get("keys") else ""))
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
                                            {"command": "approvals", "description": "Việc chờ duyệt"},
                                            {"command": "assign", "description": "Phân công agent"}])
        self._cursor("telegram.audit_cursor", "audit_log")
        self._cursor("telegram.approval_cursor", "approval_requests")
        self._engine_up = engine_running(self.s.data_dir)
        with tx(self.conn):
            audit(self.conn, "telegram", "telegram.start", chats=len(self.allowed), approvals=self.allow_approvals,
                  config_changes=self.allow_config_changes)

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
