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
    `telegram.allow_config_changes: true` (default off); each tap applies at once, is
    validated (orchestrator/overrides.py), audited as telegram:<user id> and can be undone
    (↩️ Hoàn tác) or dropped (♻️ Về gốc).
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
KEY_LABELS = {"default_worker": "worker mặc định", "workers": "thứ tự Code", "reviewers": "thứ tự Review",
              "fallback_to": "thứ tự dự phòng", "enabled": "bật/tắt", "max_concurrency": "số việc song song"}
CFG_OFF = "Thay đổi phân công qua Telegram đang tắt."
CFG_PREFIXES = {"cs:", "cl:", "ca:", "cu:", "cr:", "cg:"}
STATE_ICONS = {"provider_stop": "🟡", "cooldown": "🔴", "circuit_open": "🔴", "paused": "⏸"}
ACTION_LABELS = {"start_milestone": "Bắt đầu milestone", "create_draft_pr": "Tạo draft PR",
                 "unblock_task": "Gỡ chặn task", "other": "Cần quyết định"}
FULL_CHUNK = 3000   # raw characters per "📄 Xem đầy đủ" message (HTML escaping may grow it)


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

    # ------------------------------------------------------------ approvals
    def _approval(self, aid: int) -> sqlite3.Row | None:
        return row(self.conn, "SELECT a.*, p.slug, ms.key AS mkey, t.key AS tkey FROM approval_requests a "
                              "JOIN projects p ON p.id=a.project_id LEFT JOIN milestones ms ON ms.id=a.milestone_id "
                              "LEFT JOIN tasks t ON t.id=a.task_id WHERE a.id=?", aid)

    def approval_card(self, a: sqlite3.Row) -> str:
        """Compact card: action label, project · milestone · task, first line of the reason (≤160 chars)."""
        where = " · ".join(esc(x) for x in (a["slug"], a["mkey"], a["tkey"]) if x)
        lines = (a["reason"] or "").strip().splitlines()
        first = lines[0].strip() if lines else ""
        if len(first) > 160:
            first = first[:159] + "…"
        more = " …" if len(lines) > 1 and len(first) <= 160 and not first.endswith("…") else ""
        return (f"📝 #{a['id']} <b>{ACTION_LABELS.get(a['action'], esc(a['action']))}</b> · {where}\n"
                f"{esc(first)}{more}")

    def approval_buttons(self, aid: int, *, in_list: bool = False) -> list:
        # list buttons re-render the list; notification buttons (":n") edit that one message
        sfx, tag = ("", f" #{aid}") if in_list else (":n", "")
        kb = []
        if self.allow_approvals:
            kb.append([{"text": f"✅ Duyệt{tag}", "callback_data": f"ap:{aid}:approve{sfx}"},
                       {"text": f"❌ Từ chối{tag}", "callback_data": f"ap:{aid}:reject{sfx}"}])
        kb.append([{"text": f"📄 Xem đầy đủ{tag}", "callback_data": f"apf:{aid}"}])
        return kb

    def resolved_text(self, a: sqlite3.Row, user_id: int | None) -> str:
        ok = a["status"] == "APPROVED"
        who = "bạn" if a["resolved_by"] == f"telegram:{user_id}" else esc(a["resolved_by"] or "?")
        head = f"{'✅ Đã duyệt' if ok else '❌ Đã từ chối'} #{a['id']} bởi {who} lúc {self.t(a['resolved_at'])}"
        if a["status"] not in ("APPROVED", "REJECTED"):
            head = f"#{a['id']}: {esc(a['status'])}"
        return head + "\n" + self.approval_card(a)

    def approvals_text(self) -> tuple[str, list]:
        aps = rows(self.conn, "SELECT id FROM approval_requests WHERE status='PENDING' ORDER BY id LIMIT 10")
        if not aps:
            return "Không có gì chờ duyệt. ✅", MAIN_MENU
        lines, kb = ["<b>📝 Chờ duyệt</b>"], []
        for r in aps:
            a = self._approval(r["id"])
            lines.append("\n" + self.approval_card(a))
            buttons = [b for row_ in self.approval_buttons(a["id"], in_list=True) for b in row_]
            kb.append(buttons)
        if not self.allow_approvals:
            lines.append("\n<i>Duyệt bằng dashboard hoặc `agentbridge approvals approve ID` "
                         "(nút duyệt trên Telegram đang tắt).</i>")
        return "\n".join(lines), kb + [[{"text": "⬅️ Menu", "callback_data": "menu"}]]

    def approval_full(self, aid: int) -> list[str]:
        """Full reason plus evidence, split into messages under Telegram's limit."""
        a = self._approval(aid)
        if a is None:
            return ["Không tìm thấy yêu cầu duyệt."]
        body = f"Lý do:\n{a['reason'] or ''}"
        ev = jload(a["evidence_json"], {})
        if ev:
            import json
            body += "\n\nBằng chứng:\n" + json.dumps(ev, ensure_ascii=False, indent=1, default=str)
        status = "" if a["status"] == "PENDING" else f" ({a['status']})"
        chunks = [body[i:i + FULL_CHUNK] for i in range(0, len(body), FULL_CHUNK)] or [""]
        out = []
        for n, c in enumerate(chunks, 1):
            head = self.approval_card(a).split("\n")[0] + status
            if len(chunks) > 1:
                head += f" · phần {n}/{len(chunks)}"
            out.append(f"{head}\n\n{esc(c)}")
        return out

    def _approval_callback(self, data: str, chat_id: int, user_id: int | None,
                           message_id: int) -> tuple[str, tuple[str, list] | None]:
        """(callback answer, new content for the tapped message or None to leave it)."""
        parts = data.split(":")
        in_list = len(parts) < 4
        try:
            aid, action = int(parts[1]), parts[2]
        except (IndexError, ValueError):
            return "Nút không còn hợp lệ.", None
        a = self._approval(aid)
        if a is None:
            return "Không tìm thấy yêu cầu duyệt.", (self.approvals_text() if in_list else None)
        if a["status"] != "PENDING":
            return "Đã xử lý rồi.", (self.approvals_text() if in_list else (self.resolved_text(a, user_id), []))
        if not self.allow_approvals:
            return "Duyệt qua Telegram đang tắt.", (self.approvals_text() if in_list else None)
        from ..orchestrator.engine import Engine
        try:
            Engine(self.s, self.conn).resolve_approval(aid, action == "approve", f"telegram:{user_id}", "via Telegram")
        except (ValueError, PolicyViolation) as e:
            return f"Không thực hiện được: {e}", (self.approvals_text() if in_list else None)
        reply = f"Đã {'duyệt' if action == 'approve' else 'từ chối'} #{aid}"
        if in_list:
            return reply, self.approvals_text()
        return reply, (self.resolved_text(self._approval(aid), user_id), [])

    # ------------------------------------------------------------ assignment (⚙️ Phân công)
    # One screen per project: an ordered CODE list and an ordered REVIEW list, each tap applied at
    # once (gated by allow_config_changes, audited, ↩️ undo). callback_data uses list indices
    # (cs:<project>, cl:<project>:<w|r>:<agent>:<u|d|x>, ...) so it stays far below 64 bytes.
    def _projects(self) -> list[str]:
        return [r["slug"] for r in rows(self.conn, "SELECT slug FROM projects ORDER BY id")]

    def _agent_ids(self) -> list[str]:
        return [r["id"] for r in rows(self.conn, "SELECT id FROM agents ORDER BY id")]

    def _state_icons(self) -> dict[str, str]:
        icons = {a["id"]: STATE_ICONS.get(a["state"], "") for a in quota_report(self.conn)}
        for r in rows(self.conn, "SELECT id FROM agents WHERE enabled=0"):
            icons[r["id"]] = "⏸"
        return icons

    def assign_text(self) -> tuple[str, list]:
        projects = self._projects()
        if len(projects) == 1:
            return self.project_screen(0)
        if not projects:
            return "Chưa có dự án nào.", MAIN_MENU
        kb = [[{"text": f"📁 {slug}", "callback_data": f"cs:{i}"}] for i, slug in enumerate(projects)]
        return "<b>⚙️ Phân công</b>\nChọn dự án:", kb + [[{"text": "⬅️ Menu", "callback_data": "menu"}]]

    def project_screen(self, pi: int) -> tuple[str, list]:
        slug = self._projects()[pi]
        p = overrides.effective_project(self.conn, slug)
        icons, ids = self._state_icons(), self._agent_ids()
        edit = self.allow_config_changes
        mark = " ✏️" if p["overridden"] else ""
        lines = [f"<b>⚙️ Phân công · {esc(slug)}</b>{mark}", f"🧠 Tech Lead: {esc(p['tech_lead'])} (chỉ xem)"]
        kb: list = []
        for role, title in (("w", "🛠 CODE"), ("r", "🔍 REVIEW")):
            lst = p["workers" if role == "w" else "reviewers"]
            lines += ["", f"<b>{title}</b> (trên xuống = ưu tiên → dự phòng)"]
            lines += [f" {n}. {esc(a)} {icons.get(a, '')}".rstrip() for n, a in enumerate(lst, 1)]
            kb.append([{"text": f"— {title} —", "callback_data": "cn"}])
            for n, a in enumerate(lst, 1):
                ai = ids.index(a) if a in ids else -1
                label = {"text": f"{n}. {a} {icons.get(a, '')}".rstrip(), "callback_data": f"cg:{pi}:{ai}"}
                if edit:
                    kb.append([label] + [{"text": t, "callback_data": f"cl:{pi}:{role}:{ai}:{op}"}
                                         for t, op in (("⬆️", "u"), ("⬇️", "d"), ("➖", "x"))])
                else:
                    kb.append([label])
            if edit:
                kb.append([{"text": f"➕ Thêm vào {'Code' if role == 'w' else 'Review'}", "callback_data": f"ca:{pi}:{role}"}])
        lines += ["", "<i>Review không bao giờ giao cho người viết task. Chạm tên agent để bật/tắt, đổi số việc song song.</i>"]
        if not edit:
            lines.append("<i>Chỉ xem. Bật `telegram.allow_config_changes: true` để đổi phân công từ Telegram.</i>")
            return "\n".join(lines), kb + [[{"text": "⬅️ Menu", "callback_data": "menu"}]]
        return "\n".join(lines), kb + [[{"text": "↩️ Hoàn tác", "callback_data": f"cu:{pi}"},
                                        {"text": "♻️ Về gốc", "callback_data": f"cr:{pi}"},
                                        {"text": "⬅️ Menu", "callback_data": "menu"}]]

    def add_picker(self, pi: int, role: str) -> tuple[str, list] | None:
        slug, ids = self._projects()[pi], self._agent_ids()
        cur = overrides.effective_project(self.conn, slug)["workers" if role == "w" else "reviewers"]
        opts = [a for a in overrides.eligible(self.conn, "worker" if role == "w" else "reviewer") if a not in cur]
        if not opts:
            return None
        kb = [[{"text": f"➕ {a}", "callback_data": f"ca:{pi}:{role}:{ids.index(a)}"}] for a in opts]
        return (f"<b>➕ Thêm vào {'Code' if role == 'w' else 'Review'}</b> · {esc(slug)}\nChạm để thêm vào cuối danh sách.",
                kb + [[{"text": "⬅️ Quay lại", "callback_data": f"cs:{pi}"}]])

    def agent_screen(self, pi: int, ai: int, picker: bool = False) -> tuple[str, list]:
        aid = self._agent_ids()[ai]
        a = row(self.conn, "SELECT * FROM agents WHERE id=?", aid)
        ov = overrides.get(self.conn, "agent", aid)
        m = lambda k: " ✏️" if k in ov else ""
        rep = next((x for x in quota_report(self.conn) if x["id"] == aid), None)
        state = ""
        if rep and rep["state"] != "ok":
            state = f"\nQuota: {STATE_ICONS.get(rep['state'], '')} {esc(_stop_text(rep['reason']) if rep['state'] == 'provider_stop' else rep['state'])}"
        cfg = jload(a["config_json"], {})
        lines = [f"<b>{esc(aid)}</b> ({esc(a['provider'])}, {esc(cfg.get('model') or '-')})",
                 f"Vai trò: {esc(', '.join(jload(a['roles_json'], [])))}",
                 f"⏯ Trạng thái{m('enabled')}: {'🟢 bật' if a['enabled'] else '⏸ tắt'}",
                 f"🔢 Số việc song song{m('max_concurrency')}: {a['max_concurrency']}" + state]
        back = [[{"text": "⬅️ Phân công", "callback_data": f"cs:{pi}"}]]
        if not self.allow_config_changes:
            return "\n".join(lines), back
        if picker:
            nums = [{"text": str(n), "callback_data": f"cg:{pi}:{ai}:mc:{n}"} for n in range(overrides.MAX_CONCURRENCY + 1)]
            return ("\n".join(lines) + "\n\nChọn số việc song song:",
                    [nums[:5], nums[5:], [{"text": "⬅️ Quay lại", "callback_data": f"cg:{pi}:{ai}"}]])
        kb = [[{"text": "⏸ Tắt agent" if a["enabled"] else "▶️ Bật agent", "callback_data": f"cg:{pi}:{ai}:en"},
               {"text": f"🔢 Song song: {a['max_concurrency']}", "callback_data": f"cg:{pi}:{ai}:mc"}]]
        extra = []
        if row(self.conn, "SELECT 1 FROM override_history WHERE scope='agent' AND target=?", aid):
            extra.append({"text": "↩️ Hoàn tác", "callback_data": f"cg:{pi}:{ai}:un"})
        if ov:
            extra.append({"text": "♻️ Về gốc", "callback_data": f"cg:{pi}:{ai}:rs"})
        return "\n".join(lines), kb + ([extra] if extra else []) + back

    def _config_action(self, data: str, user_id: int | None) -> tuple[str, tuple[str, list] | None]:
        """(callback answer, new view or None) for every ⚙️ button."""
        p = data.split(":")
        actor = f"telegram:{user_id}"
        try:
            if data == "cfg":
                return "", self.assign_text()
            if p[0] == "cn":
                return "", None
            pi = int(p[1])
            slug = self._projects()[pi]
            if p[0] == "cs":
                return "", self.project_screen(pi)
            if p[0] == "cg" and len(p) == 3:
                return "", self.agent_screen(pi, int(p[2]))
            # everything below changes standing configuration
            if not self.allow_config_changes:
                return CFG_OFF, self.project_screen(pi)
            if p[0] == "cl":
                role, agent, op = p[2], self._agent_ids()[int(p[3])], {"u": "up", "d": "down", "x": "remove"}[p[4]]
                cur = overrides.effective_project(self.conn, slug)["workers" if role == "w" else "reviewers"]
                if op == "remove" and cur == [agent]:
                    return "Không thể bỏ agent cuối cùng của vai trò này.", None
                if (op == "up" and cur[:1] == [agent]) or (op == "down" and cur[-1:] == [agent]):
                    return "", None
                overrides.edit_list(self.conn, slug, "worker" if role == "w" else "reviewer", op, agent, actor)
                return "Đã lưu.", self.project_screen(pi)
            if p[0] == "ca":
                role = p[2]
                if len(p) == 3:
                    picker = self.add_picker(pi, role)
                    return ("Không còn agent phù hợp để thêm.", None) if picker is None else ("", picker)
                overrides.edit_list(self.conn, slug, "worker" if role == "w" else "reviewer", "add",
                                    self._agent_ids()[int(p[3])], actor)
                return "Đã thêm.", self.project_screen(pi)
            if p[0] == "cu":
                done = overrides.undo(self.conn, "project", slug, actor)
                return ("Đã hoàn tác." if done else "Không còn gì để hoàn tác."), self.project_screen(pi)
            if p[0] == "cr":
                keys = overrides.reset(self.conn, "project", slug, actor)
                return ("Đã về cấu hình gốc." if keys else "Đang dùng cấu hình gốc."), self.project_screen(pi)
            if p[0] == "cg":
                ai = int(p[2])
                aid = self._agent_ids()[ai]
                if p[3] == "en":
                    on = not row(self.conn, "SELECT enabled FROM agents WHERE id=?", aid)["enabled"]
                    overrides.set_override(self.conn, "agent", aid, "enabled", on, actor)
                    return ("Đã bật." if on else "Đã tắt."), self.agent_screen(pi, ai)
                if p[3] == "mc" and len(p) == 4:
                    return "", self.agent_screen(pi, ai, picker=True)
                if p[3] == "mc":
                    overrides.set_override(self.conn, "agent", aid, "max_concurrency", int(p[4]), actor)
                    return "Đã lưu.", self.agent_screen(pi, ai)
                if p[3] == "un":
                    done = overrides.undo(self.conn, "agent", aid, actor, self.s.agents)
                    return ("Đã hoàn tác." if done else "Không còn gì để hoàn tác."), self.agent_screen(pi, ai)
                if p[3] == "rs":
                    overrides.reset(self.conn, "agent", aid, actor, self.s.agents)
                    return "Đã về cấu hình gốc.", self.agent_screen(pi, ai)
        except ValueError as e:
            if str(e).startswith("invalid literal"):
                return "Nút không còn hợp lệ, mở lại.", self.assign_text()
            return f"Không lưu: {e}"[:190], None
        except (IndexError, KeyError):   # stale or forged button
            return "Nút không còn hợp lệ, mở lại.", self.assign_text()
        return "Nút không còn hợp lệ, mở lại.", self.assign_text()

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
        message_id = cq["message"]["message_id"]
        try:
            view: tuple[str, list] | None
            if data.startswith("apf:"):
                self.api("answerCallbackQuery", callback_query_id=cq["id"])
                try:
                    texts = self.approval_full(int(data[4:]))
                except ValueError:
                    texts = ["Nút không còn hợp lệ."]
                for t in texts:
                    self.send(chat_id, t)
                return
            if data.startswith("ap:"):
                reply, view = self._approval_callback(data, chat_id, user, message_id)
                self.api("answerCallbackQuery", callback_query_id=cq["id"], text=reply[:190])
            elif data == "cfg" or data[:3] in CFG_PREFIXES or data == "cn":
                reply, view = self._config_action(data, user)
                self.api("answerCallbackQuery", callback_query_id=cq["id"], **({"text": reply[:190]} if reply else {}))
            else:
                self.api("answerCallbackQuery", callback_query_id=cq["id"])
                if data.startswith("agent:"):
                    view = self.agent_text(data[6:])
                else:
                    view = {"quota": self.quota_text, "overview": self.overview_text, "refresh": self.overview_text,
                            "approvals": self.approvals_text}.get(data, lambda: ("Chọn mục:", MAIN_MENU))()
            if view is None:
                return
            text, kb = view
            self.api("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:MAX_LEN],
                     parse_mode="HTML", disable_web_page_preview=True, reply_markup={"inline_keyboard": kb})
        except RuntimeError as e:
            if "message is not modified" not in str(e):
                raise

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
        for r in rows(self.conn, "SELECT id, status FROM approval_requests WHERE id > ? ORDER BY id", cur):
            last = r["id"]
            if "approval_requested" in self.notify_kinds and r["status"] == "PENDING":
                out.append(("<b>Cần bạn duyệt</b>\n" + self.approval_card(self._approval(r["id"])),
                            self.approval_buttons(r["id"])))
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
        if act.startswith("config.") and str(e["actor"]).startswith("telegram:"):
            return None   # the tapping chat already sees the edited ⚙️ screen; every change is in the audit log
        if act == "config.undo" and "config_change" in k:
            return f"↩️ Hoàn tác phân công ({esc(e['actor'])}): {esc(d.get('target'))}"
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
