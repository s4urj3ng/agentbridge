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


def _levels(ids: list[int], parents: dict[int, list[int]]) -> dict[int, int]:
    level: dict[int, int] = {}

    def lv(i: int, seen: tuple = ()) -> int:
        if i in level:
            return level[i]
        if i in seen:
            return 0
        level[i] = 1 + max([lv(p, seen + (i,)) for p in parents.get(i, []) if p in parents] or [-1])
        return level[i]
    for i in ids:
        lv(i)
    return level


def graph_data(conn: sqlite3.Connection, milestone_id: int, *, include_cancelled: bool = False) -> dict[str, Any]:
    """Goal → Tech Lead → tasks (by dependency) → integration → PR, laid out left to right."""
    ms = row(conn, "SELECT * FROM milestones WHERE id=?", milestone_id)
    project = row(conn, "SELECT * FROM projects WHERE id=?", ms["project_id"])
    manifest = jload(project["manifest_json"], {})
    pr = milestone_progress(conn, milestone_id)
    tasks = pr["tasks"]
    if include_cancelled:
        tasks += [dict(r) | {"bucket": "cancelled"} for r in rows(
            conn, "SELECT id, key, title, state, assignee, reviewer, revision_count, blocked_reason, updated_at "
                  "FROM tasks WHERE milestone_id=? AND state='CANCELLED' ORDER BY id", milestone_id)]
    extra = {r["id"]: dict(r) for r in rows(conn, "SELECT id, pr_url, pr_number, feedback, kind FROM tasks "
                                                  "WHERE milestone_id=?", milestone_id)}
    live = {r["task_id"]: dict(r) for r in rows(
        conn, "SELECT task_id, agent_id, purpose FROM execution_attempts WHERE status IN ('STARTING','RUNNING') "
              "AND task_id IS NOT NULL")}
    ids = [t["id"] for t in tasks]
    deps = [d for d in rows(conn, "SELECT d.task_id, d.depends_on_id FROM task_dependencies d JOIN tasks t "
                                  "ON t.id=d.task_id WHERE t.milestone_id=?", milestone_id)
            if d["task_id"] in ids and d["depends_on_id"] in ids]
    parents: dict[int, list[int]] = {i: [] for i in ids}
    for d in deps:
        parents[d["task_id"]].append(d["depends_on_id"])
    has_child = {d["depends_on_id"] for d in deps}
    level = _levels(ids, parents)
    tl_running = row(conn, "SELECT 1 FROM execution_attempts WHERE purpose='tech_lead' AND status IN "
                           "('STARTING','RUNNING')")
    last_tl = row(conn, "SELECT decision_type, created_at FROM decisions WHERE milestone_id=? AND status='applied' "
                        "ORDER BY id DESC LIMIT 1", milestone_id)

    nodes = [{"id": "goal", "type": "goal", "col": 0, "title": ms["key"], "text": _short(ms["goal"], 220),
              "status": MILESTONE_VI.get(ms["status"], ms["status"]), "state": ms["status"]},
             {"id": "lead", "type": "lead", "col": 1, "title": "Tech Lead", "agent": manifest.get("project", {}).get(
                 "tech_lead"), "text": f"{ms['tl_invocations']} lần gọi" + (
                 f" · gần nhất: {last_tl['decision_type']}" if last_tl else ""), "running": bool(tl_running)}]
    for t in tasks:
        x, at = extra.get(t["id"], {}), live.get(t["id"])
        nodes.append({"id": f"t{t['id']}", "type": "task", "task_id": t["id"], "col": 2 + level[t["id"]],
                      "title": t["key"], "text": t["title"], "state": t["state"],
                      "status": STATE_VI.get(t["state"], t["state"]), "bucket": t["bucket"],
                      "assignee": t["assignee"], "reviewer": t["reviewer"], "revisions": t["revision_count"],
                      "pr_url": x.get("pr_url"), "kind": x.get("kind"),
                      "note": _short(t["blocked_reason"] or (x.get("feedback") if t["bucket"] == "revising" else ""), 160),
                      "running": (at or {}).get("agent_id"), "running_purpose": (at or {}).get("purpose")})
    last = 2 + (max(level.values()) + 1 if level else 0)
    nodes.append({"id": "integ", "type": "integration", "col": last, "title": "Tích hợp",
                  "text": ms["integration_branch"] or "chưa tạo nhánh", "status": f"{pr['done']}/{pr['total']} task",
                  "pct": pr["pct"]})
    if ms["pr_url"]:
        nodes.append({"id": "pr", "type": "pr", "col": last + 1, "title": f"PR #{ms['pr_number'] or ''}",
                      "text": "draft PR vào nhánh gốc", "url": ms["pr_url"]})
    edges = [{"from": "goal", "to": "lead"}]
    edges += [{"from": "lead", "to": f"t{i}"} for i in ids if not parents[i]]
    edges += [{"from": f"t{d['depends_on_id']}", "to": f"t{d['task_id']}"} for d in deps]
    edges += [{"from": f"t{i}", "to": "integ"} for i in ids if i not in has_child]
    if not ids:
        edges.append({"from": "lead", "to": "integ"})
    if ms["pr_url"]:
        edges.append({"from": "integ", "to": "pr"})
    # rows: centre every column vertically
    cols: dict[int, list[dict]] = {}
    for n in nodes:
        cols.setdefault(n["col"], []).append(n)
    tallest = max(len(v) for v in cols.values())
    for c, ns in cols.items():
        off = (tallest - len(ns)) / 2
        for r, n in enumerate(ns):
            n["row"] = off + r
    return {"milestone": {"id": ms["id"], "key": ms["key"], "status": ms["status"],
                          "status_vi": MILESTONE_VI.get(ms["status"], ms["status"]), "pct": pr["pct"],
                          "done": pr["done"], "total": pr["total"]},
            "project": {"slug": project["slug"], "name": project["name"]}, "nodes": nodes, "edges": edges}


DECISION_VI = {"CREATE_TASKS": "Tạo task", "REQUEST_REVISION": "Yêu cầu sửa", "APPROVE_TASK": "Duyệt task",
               "REQUEST_REVIEW": "Gửi review", "COMPLETE_MILESTONE": "Hoàn thành milestone",
               "ASSIGN_TASK": "Đổi người làm", "BLOCK_TASK": "Chặn task", "REQUEST_HUMAN_APPROVAL": "Hỏi bạn duyệt"}


def _decision_text(dtype: str, p: dict) -> str:
    if dtype == "CREATE_TASKS":
        return ", ".join(t.get("key", "?") for t in p.get("tasks", []))
    if dtype == "ASSIGN_TASK":
        return f"→ {p.get('assignee', '?')}"
    if dtype == "REQUEST_REVIEW":
        return f"→ {p.get('reviewer', '?')}" + (f" · {_short(p['focus'], 220)}" if p.get("focus") else "")
    for k in ("feedback", "reason", "summary", "note"):
        if p.get(k):
            return _short(str(p[k]), 260)
    return ""


def command_thread(conn: sqlite3.Connection, project_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """What you told the Tech Lead (goals, instructions) and what it decided, oldest first."""
    items = []
    for a in rows(conn, "SELECT * FROM audit_log WHERE project_id=? AND action IN ('goal.submit','owner.note') "
                        "ORDER BY id DESC LIMIT ?", project_id, limit):
        d = jload(a["detail_json"], {}) or {}
        items.append({"at": a["created_at"], "who": "owner", "kind": "Goal mới" if a["action"] == "goal.submit"
                      else "Chỉ thị", "text": d.get("note") or f"Milestone {d.get('milestone', '')}"})
    keys = {r["id"]: r["key"] for r in rows(conn, "SELECT id, key FROM milestones WHERE project_id=?", project_id)}
    tkeys = {r["id"]: r["key"] for r in rows(conn, "SELECT id, key FROM tasks WHERE project_id=?", project_id)}
    for dcs in rows(conn, "SELECT * FROM decisions WHERE project_id=? ORDER BY id DESC LIMIT ?", project_id, limit):
        p = jload(dcs["payload_json"], {}) or {}
        dtype = dcs["decision_type"] or "?"
        text = _decision_text(dtype, p)
        if dcs["status"] != "applied":
            text = f"(bị từ chối: {_short(dcs['rejection_reason'], 160)}) {text}"
        task = tkeys.get(p.get("task_id"), "")
        items.append({"at": dcs["created_at"], "who": "lead", "kind": DECISION_VI.get(dtype, dtype),
                      "text": (f"{task}: " if task else "") + text,
                      "milestone": keys.get(dcs["milestone_id"], ""), "ok": dcs["status"] == "applied"})
    items.sort(key=lambda i: i["at"])
    return items[-limit:]
