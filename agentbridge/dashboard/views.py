"""Data for the web dashboard: the same numbers as the Telegram bot, shaped for templates."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..db import jload, parse_ts, row, rows, utcnow
from ..reporting import milestone_progress, quota_report

STATE_VI = {"CREATED": "mới tạo", "QUEUED": "trong hàng đợi", "DISPATCHED": "đang giao", "RUNNING": "đang làm",
            "VALIDATING": "đang chạy test", "REVIEW_PENDING": "chờ review", "REVISION_REQUIRED": "cần sửa",
            "APPROVED": "đã duyệt", "COMPLETED": "xong", "BLOCKED": "bị chặn", "FAILED": "thất bại",
            "CANCELLED": "đã huỷ"}
MILESTONE_VI = {"ACTIVE": "đang chạy", "BLOCKED": "đang dừng", "AWAITING_APPROVAL": "chờ duyệt bắt đầu",
                "COMPLETED": "đã xong", "CANCELLED": "đã huỷ"}
BUCKET_VI = {"done": "xong", "running": "đang làm", "review": "chờ review", "revising": "cần sửa",
             "waiting": "chờ", "blocked": "bị chặn"}
ROLE_GROUPS = [("tech_lead", "Tech Lead"), ("worker", "Code"), ("reviewer", "Review")]
QUOTA_STATE_VI = {"ok": "sẵn sàng", "cooldown": "tạm nghỉ", "circuit_open": "lỗi liên tiếp, tạm ngắt",
                  "paused": "tạm dừng", "provider_stop": "gần hết quota"}


class Clock:
    """Local time for humans (telegram.timezone, default Asia/Seoul)."""

    def __init__(self, tz: str):
        self.tz = ZoneInfo(tz)

    def __call__(self, ts: str | None, with_date: bool | None = None) -> str:
        d = parse_ts(ts) if isinstance(ts, str) else ts
        if d is None:
            return "—"
        local = d.astimezone(self.tz)
        same_day = local.date() == datetime.now(self.tz).date()
        return local.strftime("%H:%M" if (with_date is False or (with_date is None and same_day)) else "%d/%m %H:%M")

    def until(self, ts: str | None) -> str:
        d = parse_ts(ts) if isinstance(ts, str) else ts
        if d is None:
            return ""
        mins = int((d - utcnow()).total_seconds() // 60)
        if mins <= 0:
            return "đã reset"
        return f"{mins} phút" if mins < 60 else (f"{mins // 60}h{mins % 60:02d}" if mins < 24 * 60 else self(ts, True))


def progress_cards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open milestones (or each project's last completed one) with their progress."""
    out = []
    for p in rows(conn, "SELECT id, slug, name, paused FROM projects ORDER BY id"):
        mss = rows(conn, "SELECT * FROM milestones WHERE project_id=? AND status IN "
                         "('ACTIVE','AWAITING_APPROVAL','BLOCKED') ORDER BY id", p["id"])
        if not mss:
            mss = rows(conn, "SELECT * FROM milestones WHERE project_id=? AND status='COMPLETED' "
                             "ORDER BY completed_at DESC LIMIT 1", p["id"])
        for m in mss:
            pr = milestone_progress(conn, m["id"])
            out.append({"project": dict(p), "milestone": dict(m), "status_vi": MILESTONE_VI.get(m["status"], m["status"]),
                        **pr})
    return out


def quota_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    roles = {r["id"]: jload(r["roles_json"], []) for r in rows(conn, "SELECT id, roles_json FROM agents")}
    agents = quota_report(conn)
    groups = []
    for role, label in ROLE_GROUPS:
        members = [a | {"state_vi": QUOTA_STATE_VI.get(a["state"], a["state"])} for a in agents
                   if role in roles.get(a["id"], [])]
        if members:
            groups.append({"role": role, "label": label, "agents": members})
    return groups


def _short(text: str | None, n: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def activity(conn: sqlite3.Connection, limit: int = 40) -> list[dict[str, Any]]:
    """Recent audit entries as one human line each (noisy quota/probe rows are skipped)."""
    keys = {r["id"]: r["key"] for r in rows(conn, "SELECT id, key FROM tasks")}
    out = []
    for e in rows(conn, "SELECT * FROM audit_log WHERE action NOT LIKE 'quota.%' AND action NOT LIKE 'probe.%' "
                        "ORDER BY id DESC LIMIT ?", limit):
        d = jload(e["detail_json"], {}) or {}
        key = keys.get(e["task_id"], f"#{e['task_id']}") if e["task_id"] else ""
        kind, text = "info", e["action"]
        if e["action"] == "task.transition":
            dst = d.get("dst", "")
            kind = {"COMPLETED": "ok", "APPROVED": "ok", "BLOCKED": "bad", "FAILED": "bad",
                    "REVISION_REQUIRED": "warn", "REVIEW_PENDING": "review"}.get(dst, "info")
            text = f"{key} → {STATE_VI.get(dst, dst)}"
            if d.get("reason") and dst in ("BLOCKED", "FAILED", "REVISION_REQUIRED", "REVIEW_PENDING", "QUEUED"):
                text += f" · {_short(d['reason'])}"
        elif e["action"] == "review.request":
            kind, text = "review", f"Yêu cầu review {key}"
        elif e["action"] == "approval.resolve":
            kind = "ok" if d.get("approved") else "bad"
            text = f"{'Đã duyệt' if d.get('approved') else 'Đã từ chối'} yêu cầu #{d.get('approval_id')}"
        elif e["action"] == "task.replace":
            text = f"{d.get('old')} được làm lại bằng {d.get('new')}"
        elif e["action"] == "milestone.resume":
            kind, text = "ok", "Milestone chạy lại"
        elif e["action"] == "owner.note":
            text = "Ghi chú của bạn gửi Tech Lead"
        elif e["action"].startswith("config."):
            text = f"Đổi phân công ({e['action'][7:]})"
        elif e["action"] == "auth.login_failed":
            kind, text = "bad", f"Đăng nhập sai từ {d.get('ip', '?')}"
        elif e["action"] == "auth.login":
            text = f"Đăng nhập dashboard từ {d.get('ip', '?')}"
        out.append({"at": e["created_at"], "actor": e["actor"], "kind": kind, "text": text, "task_id": e["task_id"]})
    return out


def counters(conn: sqlite3.Connection) -> dict[str, int]:
    def c(sql: str) -> int:
        return row(conn, sql)["c"]
    return {"running": c("SELECT COUNT(*) c FROM tasks WHERE state IN ('DISPATCHED','RUNNING','VALIDATING')"),
            "review": c("SELECT COUNT(*) c FROM tasks WHERE state='REVIEW_PENDING'"),
            "queued": c("SELECT COUNT(*) c FROM tasks WHERE state IN ('CREATED','QUEUED','REVISION_REQUIRED')"),
            "blocked": c("SELECT COUNT(*) c FROM tasks t JOIN milestones m ON m.id=t.milestone_id WHERE "
                         "t.state='BLOCKED' AND m.status IN ('ACTIVE','BLOCKED')"),
            "approvals": c("SELECT COUNT(*) c FROM approval_requests WHERE status='PENDING'"),
            "attempts_24h": c("SELECT COUNT(*) c FROM execution_attempts WHERE started_at >= "
                              "strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day')")}
