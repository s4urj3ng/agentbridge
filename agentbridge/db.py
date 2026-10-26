"""SQLite access: WAL mode, migrations, small helpers.

Every orchestrator component opens its own connection; SQLite WAL gives us
concurrent readers with a single writer, which is all v1 needs. Writes that
must be atomic use `with tx(conn):` (BEGIN IMMEDIATE) so that claim/transition
logic cannot interleave between processes.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(offset_s: float = 0) -> str:
    return (utcnow() + timedelta(seconds=offset_s)).isoformat(timespec="seconds")


def parse_ts(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE transaction: takes the write lock up front."""
    if conn.in_transaction:  # nested use: join the outer transaction
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for f in sorted(migrations_dir.glob("*.sql")):
        if f.stem in applied:
            continue
        with tx(conn):
            # executescript would COMMIT implicitly; run statements one by one instead.
            for stmt in _split_sql(f.read_text()):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (f.stem, now_iso()))
        done.append(f.stem)
    return done


def _split_sql(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";") and sqlite3.complete_statement("\n".join(buf)):
            out.append("\n".join(buf))
            buf = []
    if "".join(buf).strip():
        out.append("\n".join(buf))
    return out


def jdump(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def jload(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


def row(conn: sqlite3.Connection, sql: str, *args: Any) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def rows(conn: sqlite3.Connection, sql: str, *args: Any) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


def audit(conn: sqlite3.Connection, actor: str, action: str, /, *, project_id: int | None = None,
          task_id: int | None = None, **detail: Any) -> None:
    from .policies.safety import redact
    conn.execute(
        "INSERT INTO audit_log (actor, action, project_id, task_id, detail_json, created_at) VALUES (?,?,?,?,?,?)",
        (actor, action, project_id, task_id, redact(jdump(detail)), now_iso()))
