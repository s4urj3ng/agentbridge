"""Task state machine.

All state changes go through `transition()`, which performs a compare-and-set
on (state, state_version). A transition that is not in ALLOWED raises; a CAS
miss (someone else moved the task first) raises StaleTransition. Neither can
silently mark work COMPLETED.
"""
from __future__ import annotations

import sqlite3
from enum import StrEnum

from ..db import audit, now_iso, tx


class TaskState(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


S = TaskState
TERMINAL = {S.COMPLETED, S.FAILED, S.CANCELLED}
ACTIVE_EXECUTION = {S.DISPATCHED, S.RUNNING, S.VALIDATING}

ALLOWED: dict[TaskState, set[TaskState]] = {
    S.CREATED: {S.QUEUED, S.BLOCKED, S.CANCELLED},
    S.QUEUED: {S.DISPATCHED, S.BLOCKED, S.CANCELLED},
    S.DISPATCHED: {S.RUNNING, S.QUEUED, S.FAILED, S.CANCELLED},
    # RUNNING may go back to QUEUED (lost process / quota requeue) but never
    # directly to COMPLETED or APPROVED: results must pass VALIDATING first.
    S.RUNNING: {S.VALIDATING, S.QUEUED, S.BLOCKED, S.FAILED, S.CANCELLED},
    S.VALIDATING: {S.REVIEW_PENDING, S.REVISION_REQUIRED, S.BLOCKED, S.FAILED, S.QUEUED, S.CANCELLED},
    S.REVIEW_PENDING: {S.APPROVED, S.REVISION_REQUIRED, S.BLOCKED, S.CANCELLED},
    S.REVISION_REQUIRED: {S.QUEUED, S.BLOCKED, S.FAILED, S.CANCELLED},
    S.APPROVED: {S.COMPLETED, S.BLOCKED, S.REVISION_REQUIRED},
    S.BLOCKED: {S.QUEUED, S.CANCELLED, S.FAILED, S.REVIEW_PENDING},
    S.COMPLETED: set(),
    S.FAILED: set(),
    S.CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


class StaleTransition(Exception):
    pass


def can_transition(src: str, dst: str) -> bool:
    return TaskState(dst) in ALLOWED[TaskState(src)]


def transition(conn: sqlite3.Connection, task_id: int, dst: TaskState | str, *, actor: str,
               reason: str = "", expect: TaskState | str | set | None = None, **fields) -> str:
    """Move a task to `dst`. Returns the previous state.

    `expect` restricts the allowed source state(s). `fields` are extra columns
    updated atomically with the state change.
    """
    dst = TaskState(dst)
    with tx(conn):
        r = conn.execute("SELECT state, state_version, project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if r is None:
            raise InvalidTransition(f"task {task_id} does not exist")
        src = TaskState(r["state"])
        if expect is not None:
            exp = {TaskState(e) for e in (expect if isinstance(expect, (set, list, tuple)) else [expect])}
            if src not in exp:
                raise StaleTransition(f"task {task_id} is {src}, expected {sorted(exp)}")
        if dst not in ALLOWED[src]:
            raise InvalidTransition(f"task {task_id}: {src} -> {dst} is not allowed")
        sets = ["state=?", "state_version=state_version+1", "updated_at=?"]
        args: list = [dst.value, now_iso()]
        for k, v in fields.items():
            if not k.isidentifier():
                raise ValueError(k)
            sets.append(f"{k}=?")
            args.append(v)
        args += [task_id, r["state_version"]]
        cur = conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=? AND state_version=?", args)
        if cur.rowcount != 1:
            raise StaleTransition(f"task {task_id} changed concurrently")
        conn.execute(
            "INSERT INTO task_transitions (task_id, from_state, to_state, reason, actor, created_at) VALUES (?,?,?,?,?,?)",
            (task_id, src.value, dst.value, reason[:2000], actor, now_iso()))
        audit(conn, actor, "task.transition", project_id=r["project_id"], task_id=task_id,
              src=src.value, dst=dst.value, reason=reason[:500])
    return src.value
