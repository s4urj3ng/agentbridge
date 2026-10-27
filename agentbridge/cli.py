"""`agentbridge` management CLI."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path

from .config import load_settings
from .db import connect, migrate, row, rows
from .orchestrator.engine import Engine
from .policies.manifest import Manifest
from .policies.safety import PolicyViolation


def _engine(args) -> Engine:
    s = load_settings(args.config, args.data_dir)
    e = Engine(s)
    e.sync_agents()
    return e


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_init(args):
    e = _engine(args)
    print(f"database: {e.s.db_path}\nagents: {[r['id'] for r in rows(e.conn, 'SELECT id FROM agents')]}")


def cmd_project_add(args):
    e = _engine(args)
    pid = e.register_project(Manifest.load(args.manifest))
    print(f"project registered: id={pid}")


def cmd_project_list(args):
    e = _engine(args)
    _print([dict(r) for r in rows(e.conn, "SELECT id, slug, name, repo_path, base_branch, paused FROM projects")])


def cmd_project_pause(args, paused=True):
    e = _engine(args)
    e.conn.execute("UPDATE projects SET paused=? WHERE slug=?", (int(paused), args.slug))
    print(("paused " if paused else "resumed ") + args.slug)


def cmd_goal(args):
    e = _engine(args)
    try:
        mid = e.submit_goal(args.project, args.goal, args.milestone)
    except PolicyViolation as ex:
        sys.exit(f"rejected by policy: {ex}")
    print(f"milestone created: id={mid}")


def cmd_run(args):
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    e = _engine(args)
    asyncio.run(e.run(until_idle=args.until_idle, max_seconds=args.max_seconds))
    if args.until_idle:
        _print(e.status())


def cmd_status(args):
    _print(_engine(args).status())


def cmd_tasks(args):
    e = _engine(args)
    _print([dict(r) for r in rows(e.conn, "SELECT id, key, state, assignee, revision_count, validation_retries, "
                                         "attempt_count, branch, blocked_reason FROM tasks ORDER BY id")])


def cmd_events(args):
    e = _engine(args)
    for r in rows(e.conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", args.n)[::-1]:
        print(f"{r['created_at']} {r['actor']:<12} {r['action']:<22} task={r['task_id']} {r['detail_json'][:160]}")


def cmd_agents(args):
    e = _engine(args)
    known = [r["id"] for r in rows(e.conn, "SELECT id FROM agents ORDER BY id")]
    if args.agent and args.agent not in known:
        sys.exit(f"unknown agent {args.agent!r}. Known: {', '.join(known)}\n"
                 "(identities come from the config file; edit it and restart the services)")
    if args.action == "list":
        _print([dict(r) | {"roles": json.loads(r["roles_json"])} for r in rows(
            e.conn, "SELECT id, provider, roles_json, enabled, paused, max_concurrency, daily_invocation_limit, "
                    "cooldown_until, cooldown_reason FROM agents ORDER BY id")])
    elif args.action == "detect":
        _print(e.detect_agents())
    elif args.action in ("enable", "disable"):
        e.conn.execute("UPDATE agents SET enabled=? WHERE id=?", (int(args.action == "enable"), args.agent))
        print(f"{args.action}d {args.agent}")
    elif args.action in ("pause", "resume"):
        from .orchestrator import quota
        quota.set_paused(e.conn, args.agent, args.action == "pause", "owner:cli")
        print(f"{args.action}d {args.agent}")
    elif args.action == "smoke":
        _print(asyncio.run(_smoke(e, args.agent)))
    elif args.action == "concurrency":
        e.conn.execute("UPDATE agents SET max_concurrency=? WHERE id=?", (int(args.value), args.agent))


async def _smoke(e: Engine, agent_id: str) -> dict:
    """One minimal schema-constrained call (spends one real invocation of that agent's quota)."""
    import time
    from .adapters.base import ExecRequest
    ad = e.adapter(agent_id)
    cap = ad.detect()
    if not cap.available:
        return {"agent": agent_id, "ok": False, "reason": "not available", "notes": cap.notes}
    run_dir = e.s.artifacts_dir / "smoke" / f"{agent_id}-{int(time.time())}"
    cwd = e.s.data_dir / "smoke-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    schema = {"type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]},
                                               "sum": {"type": "integer"}},
              "required": ["status", "sum"], "additionalProperties": False}
    res = await ad.execute(ExecRequest("tech_lead", "Do not use tools. Compute 2+3 and reply with status ok and the sum.",
                                       cwd, run_dir, 180, schema))
    return {"agent": agent_id, "ok": res.outcome == "success" and (res.structured or {}).get("sum") == 5,
            "outcome": res.outcome, "structured": res.structured, "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens, "duration_s": res.duration_s, "detail": res.detail[:300]}


def cmd_approvals(args):
    e = _engine(args)
    if args.action == "list":
        _print([dict(r) for r in rows(e.conn, "SELECT id, action, status, task_id, milestone_id, reason, requested_by "
                                             "FROM approval_requests ORDER BY id DESC LIMIT 50")])
    else:
        e.resolve_approval(int(args.id), args.action == "approve", "owner:cli", args.note or "")
        print(f"{args.action}d approval {args.id}")


def cmd_demo(args):
    from .demo import create_demo_repo, write_demo_manifest
    s = load_settings(args.config, args.data_dir)
    s.ensure_dirs()
    repo = create_demo_repo(s.data_dir / "demo-repo")
    manifest = write_demo_manifest(repo, s.data_dir / "demo-manifest.yaml")
    e = Engine(s)
    e.sync_agents()
    e.register_project(Manifest.load(manifest))
    mid = e.submit_goal("demo-calc", "Fix add() and implement mul() in calc.py with tests", args.milestone)
    print(f"demo repo: {repo}\nmanifest: {manifest}\nmilestone id: {mid}\nrunning orchestrator until idle ...")
    asyncio.run(e.run(until_idle=True, max_seconds=args.max_seconds))
    for t in rows(e.conn, "SELECT id, key, state, assignee, revision_count, validation_retries FROM tasks "
                          "WHERE milestone_id=?", mid):
        print(f"  task {t['id']} {t['key']:<10} {t['state']:<10} by {t['assignee']} "
              f"revisions={t['revision_count']} validation_retries={t['validation_retries']}")
    ms = row(e.conn, "SELECT * FROM milestones WHERE id=?", mid)
    print(f"milestone {ms['key']}: {ms['status']} (integration branch {ms['integration_branch']})")
    print("tech lead invocations:", ms["tl_invocations"])
    print("pending approvals:", [dict(r) for r in rows(e.conn, "SELECT id, action FROM approval_requests "
                                                             "WHERE status='PENDING'")])


def cmd_serve(args):
    import uvicorn
    from .api import create_app
    s = load_settings(args.config, args.data_dir)
    if not s.admin_token:
        if args.dev and s.bind_host in ("127.0.0.1", "localhost"):
            tok = secrets.token_urlsafe(24)
            import os
            os.environ["AGENTBRIDGE_ADMIN_TOKEN"] = tok
            print(f"DEV MODE: generated admin token for this run: {tok}")
        else:
            sys.exit("AGENTBRIDGE_ADMIN_TOKEN must be set (or use --dev on localhost)")
    uvicorn.run(create_app(s), host=args.host or s.bind_host, port=args.port or s.port, log_level="info")


def cmd_telegram(args):
    from .integrations.telegram import TelegramBot
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines would include the bot token URL
    s = load_settings(args.config, args.data_dir)
    s.ensure_dirs()
    migrate(connect(s.db_path))
    bot = TelegramBot(s)
    if args.discover:
        found = bot.discover()
        _print(found or "no messages yet: send /start to the bot from Telegram, then run this again")
        return
    if args.test:
        bot.broadcast("✅ AgentBridge Telegram bot hoạt động. Gõ /status hoặc /quota.")
        print(f"test message sent to {len(bot.allowed)} chat(s)")
        return
    bot.run_forever()


def cmd_backup(args):
    s = load_settings(args.config, args.data_dir)
    src = connect(s.db_path)
    dest = Path(args.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    src.backup(out)
    out.close()
    print(f"backup written: {dest} (provider credentials are never stored in the database)")


def cmd_restore(args):
    s = load_settings(args.config, args.data_dir)
    if (s.data_dir / "engine.lock").exists():
        import fcntl
        with open(s.data_dir / "engine.lock") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                sys.exit("engine is running; stop it before restoring")
    if s.db_path.exists() and not args.force:
        sys.exit(f"{s.db_path} exists; pass --force to replace it (a copy is kept as .pre-restore)")
    if s.db_path.exists():
        shutil.copy2(s.db_path, s.db_path.with_suffix(".db.pre-restore"))
        for ext in ("-wal", "-shm"):
            Path(str(s.db_path) + ext).unlink(missing_ok=True)
    s.ensure_dirs()
    src = sqlite3.connect(args.src)
    out = sqlite3.connect(str(s.db_path))
    src.backup(out)
    out.close()
    migrate(connect(s.db_path))
    print(f"restored {args.src} → {s.db_path}")


def cmd_recover(args):
    e = _engine(args)
    e.acquire_lock()
    _print(e.recover())


def cmd_merge(args):
    e = _engine(args)
    try:
        e.request_merge(args.project, args.source, args.target, "owner:cli")
    except PolicyViolation as ex:
        sys.exit(f"blocked: {ex}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agentbridge")
    ap.add_argument("--config", default=None, help="config YAML (default: $AGENTBRIDGE_CONFIG or ./agentbridge.yaml)")
    ap.add_argument("--data-dir", default=None, help="data dir (default: $AGENTBRIDGE_DATA_DIR or ./var)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    p = sub.add_parser("project")
    ps = p.add_subparsers(dest="action", required=True)
    x = ps.add_parser("add"); x.add_argument("manifest"); x.set_defaults(fn=cmd_project_add)
    ps.add_parser("list").set_defaults(fn=cmd_project_list)
    x = ps.add_parser("pause"); x.add_argument("slug"); x.set_defaults(fn=lambda a: cmd_project_pause(a, True))
    x = ps.add_parser("resume"); x.add_argument("slug"); x.set_defaults(fn=lambda a: cmd_project_pause(a, False))
    x = sub.add_parser("goal"); x.add_argument("project"); x.add_argument("goal"); x.add_argument("--milestone")
    x.set_defaults(fn=cmd_goal)
    x = sub.add_parser("run", help="host worker: execute the orchestration loop")
    x.add_argument("--until-idle", action="store_true"); x.add_argument("--max-seconds", type=float)
    x.add_argument("-v", "--verbose", action="store_true"); x.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("tasks").set_defaults(fn=cmd_tasks)
    x = sub.add_parser("events"); x.add_argument("-n", type=int, default=40); x.set_defaults(fn=cmd_events)
    x = sub.add_parser("agents")
    x.add_argument("action", choices=["list", "detect", "smoke", "enable", "disable", "pause", "resume", "concurrency"])
    x.add_argument("agent", nargs="?"); x.add_argument("value", nargs="?"); x.set_defaults(fn=cmd_agents)
    x = sub.add_parser("approvals"); x.add_argument("action", choices=["list", "approve", "reject"])
    x.add_argument("id", nargs="?"); x.add_argument("--note"); x.set_defaults(fn=cmd_approvals)
    x = sub.add_parser("demo", help="run the synthetic mock-provider demo")
    x.add_argument("--milestone", default=None); x.add_argument("--max-seconds", type=float, default=300)
    x.set_defaults(fn=cmd_demo)
    x = sub.add_parser("serve", help="API + dashboard (control plane)")
    x.add_argument("--host"); x.add_argument("--port", type=int); x.add_argument("--dev", action="store_true")
    x.set_defaults(fn=cmd_serve)
    x = sub.add_parser("telegram", help="Telegram bot: quota/progress buttons and notifications")
    x.add_argument("--discover", action="store_true", help="list chat ids that messaged the bot")
    x.add_argument("--test", action="store_true", help="send a test message to the allowed chats")
    x.set_defaults(fn=cmd_telegram)
    x = sub.add_parser("backup"); x.add_argument("dest"); x.set_defaults(fn=cmd_backup)
    x = sub.add_parser("restore"); x.add_argument("src"); x.add_argument("--force", action="store_true")
    x.set_defaults(fn=cmd_restore)
    sub.add_parser("recover").set_defaults(fn=cmd_recover)
    x = sub.add_parser("merge", help="(always blocked for protected branches in v1)")
    x.add_argument("project"); x.add_argument("source"); x.add_argument("target"); x.set_defaults(fn=cmd_merge)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
