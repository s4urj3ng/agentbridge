"""Acceptance 9: state persists through restart; crashes never fake completion."""
from __future__ import annotations

import asyncio
import os
import signal
import time

from agentbridge.workers import process

from .conftest import make_engine, q, run


def _slow_worker(settings, seconds=3.0):
    for a in settings.agents:
        if a["id"] == "mock-worker":
            a["sleep"] = seconds


async def _run_until(engine, predicate, timeout=30):
    engine.acquire_lock()
    engine.sync_agents()
    engine.recover()
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            await engine.tick()
            if predicate():
                return True
            await asyncio.sleep(0.05)
        return False
    finally:
        # simulate a crash: abandon in-flight jobs without letting them finish
        for j in engine.jobs.values():
            j.cancel()
        await asyncio.sleep(0)
        engine.release_lock()
        engine.conn.close()


def _running_attempt(engine):
    r = q(engine, "SELECT * FROM execution_attempts WHERE purpose='work' AND status='RUNNING'")
    return r[0] if r else None


def test_restart_adopts_finished_or_running_worker(settings, tmp_path):
    from .conftest import register
    _slow_worker(settings, 2.0)
    e1 = make_engine(settings)
    register(e1, tmp_path)
    mid = e1.submit_goal("demo-calc", "Fix add() and implement mul()", "R1")
    assert asyncio.run(_run_until(e1, lambda: _running_attempt(e1) is not None))

    # "reboot": a brand-new engine on the same database
    e2 = make_engine(settings)
    st = q(e2, "SELECT state FROM tasks WHERE key='fix-add'")[0]["state"]
    assert st == "RUNNING"  # persisted, and not marked completed by the crash
    notes = e2.recover()
    assert any("adopt" in n for n in notes)
    run(e2, 180)
    assert q(e2, "SELECT status FROM milestones WHERE id=?", mid)[0]["status"] == "COMPLETED"
    # the adopted attempt was collected, not duplicated
    first = q(e2, "SELECT * FROM execution_attempts WHERE purpose='work' ORDER BY id LIMIT 1")[0]
    assert first["status"] == "COLLECTED" and first["outcome"] == "success"


def test_lost_process_is_requeued_never_completed(settings, tmp_path):
    from .conftest import register
    _slow_worker(settings, 30.0)
    e1 = make_engine(settings)
    register(e1, tmp_path)
    e1.submit_goal("demo-calc", "Fix add() and implement mul()", "R2")
    assert asyncio.run(_run_until(e1, lambda: _running_attempt(e1) is not None))
    e2 = make_engine(settings)
    a = _running_attempt(e2)
    # kill the wrapper and its child: the process is gone with no exit record
    os.killpg(a["pid"], signal.SIGKILL)
    for _ in range(50):
        process.reap(a["pid"])
        if not process.pid_alive_with_nonce(a["pid"], a["nonce"]):
            break
        time.sleep(0.1)
    child_pids = os.popen(f"pgrep -f 'mock_agent --purpose work'").read().split()
    for p in child_pids:
        try:
            os.kill(int(p), signal.SIGKILL)
        except ProcessLookupError:
            pass
    notes = e2.recover()
    assert any("lost" in n for n in notes)
    att = q(e2, "SELECT status, outcome FROM execution_attempts WHERE id=?", a["id"])[0]
    assert att == {"status": "LOST", "outcome": "lost"}
    t = q(e2, "SELECT state FROM tasks WHERE id=?", a["task_id"])[0]
    assert t["state"] == "QUEUED"
    assert not q(e2, "SELECT 1 FROM task_transitions WHERE to_state='COMPLETED'")


def test_event_queue_is_idempotent(engine, demo_project):
    from agentbridge.db import tx
    from agentbridge.orchestrator import events as evq
    with tx(engine.conn):
        a = evq.emit(engine.conn, "MILESTONE_READY", project_id=1, payload={}, dedupe_key="dup:1")
        b = evq.emit(engine.conn, "MILESTONE_READY", project_id=1, payload={}, dedupe_key="dup:1")
    assert a is not None and b is None
    assert q(engine, "SELECT COUNT(*) c FROM events WHERE dedupe_key='dup:1'")[0]["c"] == 1


def test_claim_is_transaction_safe(engine, demo_project):
    """Two engines/connections racing to dispatch the same task: only one wins."""
    from agentbridge.db import connect, now_iso
    from agentbridge.orchestrator.states import StaleTransition, TaskState, transition
    engine.submit_goal("demo-calc", "x task", "C1")
    mid = q(engine, "SELECT id FROM milestones")[0]["id"]
    engine.conn.execute("INSERT INTO tasks (project_id, milestone_id, key, title, description, assignee, state, "
                        "created_at, updated_at) VALUES (1, ?, 'k', 'title', 'desc', 'mock-worker', 'QUEUED', ?, ?)",
                        (mid, now_iso(), now_iso()))
    tid = q(engine, "SELECT id FROM tasks WHERE key='k'")[0]["id"]
    c2 = connect(engine.s.db_path)
    transition(engine.conn, tid, TaskState.DISPATCHED, actor="a", expect=TaskState.QUEUED)
    try:
        transition(c2, tid, TaskState.DISPATCHED, actor="b", expect=TaskState.QUEUED)
        raised = False
    except StaleTransition:
        raised = True
    assert raised
    assert q(engine, "SELECT COUNT(*) c FROM task_transitions WHERE task_id=? AND to_state='DISPATCHED'", tid)[0]["c"] == 1


def test_single_engine_lock(settings):
    from agentbridge.orchestrator.engine import EngineLocked
    e1, e2 = make_engine(settings), make_engine(settings)
    e1.acquire_lock()
    try:
        raised = False
        try:
            e2.acquire_lock()
        except EngineLocked:
            raised = True
        assert raised
    finally:
        e1.release_lock()
