"""Control plane: JSON API, server-rendered dashboard, SSE and GitHub webhooks.

This process never executes agents. It reads state and writes owner intents
(goals, approvals, pause/resume) that the host engine acts on.

Auth: every route except /api/health, /login and /webhooks/github requires
either `Authorization: Bearer $AGENTBRIDGE_ADMIN_TOKEN` or a signed session
cookie obtained by logging in with that token. Cookie-authenticated POSTs must
carry a CSRF token. Bind to 127.0.0.1 and reach it via Tailscale, or publish it
through a Cloudflare Tunnel (docs/DASHBOARD.md): failed logins are throttled per
client IP and the session cookie is marked Secure behind an HTTPS proxy.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .config import Settings
from .dashboard import views
from .db import audit, connect, jload, migrate, now_iso, row, rows, tx
from .integrations.github import record_delivery, verify_signature
from .orchestrator import events as evq
from .orchestrator import quota
from .orchestrator.engine import Engine
from .policies.safety import PolicyViolation, read_confined, redact

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "dashboard" / "templates"))
SESSION_TTL = 12 * 3600
LOGIN_WINDOW_S, LOGIN_MAX_FAILURES = 15 * 60, 5     # per client IP
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "same-origin",
                    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                               "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                               "frame-ancestors 'none'; form-action 'self'"}


class GoalIn(BaseModel):
    goal: str = Field(min_length=3, max_length=8000)
    milestone: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-\.]{1,40}$")


class MergeIn(BaseModel):
    source: str
    target: str


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_dirs()
    app = FastAPI(title="AgentBridge", docs_url=None, redoc_url=None, openapi_url=None)
    conn = connect(settings.db_path)
    migrate(conn)
    engine = Engine(settings, conn)   # used for DB-only owner operations; never .run() here
    engine.sync_agents()
    secret = settings.session_secret.encode()
    clock = views.Clock((settings.telegram or {}).get("timezone", "Asia/Seoul"))
    TEMPLATES.env.globals.update(t=clock, until=clock.until, STATE_VI=views.STATE_VI, BUCKET_VI=views.BUCKET_VI)
    failures: dict[str, list[float]] = {}

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if is_https(request):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    def is_https(request: Request) -> bool:
        # cloudflared / a local reverse proxy terminates TLS and connects to 127.0.0.1
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"

    def client_ip(request: Request) -> str:
        return (request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?"))[:64]

    def throttled(ip: str) -> bool:
        now = time.time()
        if len(failures) > 10_000:          # many rotating IPs: drop entries outside the window
            for k in [k for k, v in failures.items() if not v or now - v[-1] >= LOGIN_WINDOW_S]:
                del failures[k]
        recent = [x for x in failures.get(ip, []) if now - x < LOGIN_WINDOW_S]
        if recent:
            failures[ip] = recent
        else:
            failures.pop(ip, None)
        return len(recent) >= LOGIN_MAX_FAILURES

    # ------------------------------------------------------------ auth
    def sign(value: str) -> str:
        return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()

    def session_valid(cookie: str | None) -> bool:
        if not cookie or "." not in cookie:
            return False
        exp, sig = cookie.split(".", 1)
        return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, sign("admin:" + exp))

    def csrf_for(cookie: str) -> str:
        return sign("csrf:" + cookie)[:32]

    async def auth(request: Request) -> str:
        token = settings.admin_token
        if not token:
            raise HTTPException(503, "AGENTBRIDGE_ADMIN_TOKEN not configured")
        h = request.headers.get("authorization", "")
        if h.startswith("Bearer ") and hmac.compare_digest(h[7:], token):
            return "owner:api"
        cookie = request.cookies.get("ab_session")
        if session_valid(cookie):
            if request.method == "POST":
                form = await request.form() if "form" in request.headers.get("content-type", "") else {}
                sent = request.headers.get("x-csrf-token") or form.get("csrf", "")
                if not hmac.compare_digest(str(sent), csrf_for(cookie)):
                    raise HTTPException(403, "CSRF token missing or invalid")
            return "owner:dashboard"
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            raise HTTPException(307, headers={"Location": "/login"})
        raise HTTPException(401, "authentication required")

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        cookie = request.cookies.get("ab_session", "")
        ctx.update(request=request, csrf=csrf_for(cookie) if cookie else "", nav=name.split(".")[0],
                   pending=row(conn, "SELECT COUNT(*) c FROM approval_requests WHERE status='PENDING'")["c"],
                   engine_up=health()["engine_running"])
        return TEMPLATES.TemplateResponse(request, name, ctx)

    def q(sql: str, *a) -> list[dict]:
        return [dict(r) for r in rows(conn, sql, *a)]

    # ------------------------------------------------------------ public
    @app.get("/api/health")
    def health():
        ok = row(conn, "SELECT 1 AS ok")["ok"] == 1
        lock = settings.data_dir / "engine.lock"
        engine_up = False
        if lock.exists():
            import fcntl
            with open(lock) as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except BlockingIOError:
                    engine_up = True
        return {"status": "ok" if ok else "degraded", "db": ok, "engine_running": engine_up, "time": now_iso()}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return TEMPLATES.TemplateResponse(request, "login.html", {"request": request, "error": None, "nav": "login"})

    @app.post("/login")
    def login(request: Request, token: str = Form(...)):
        ip = client_ip(request)
        if throttled(ip):
            return TEMPLATES.TemplateResponse(request, "login.html", {
                "request": request, "nav": "login",
                "error": "Sai quá nhiều lần. Thử lại sau 15 phút."}, status_code=429)
        if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
            failures.setdefault(ip, []).append(time.time())
            with tx(conn):
                audit(conn, "anonymous", "auth.login_failed", ip=ip)
            return TEMPLATES.TemplateResponse(request, "login.html", {"request": request, "error": "Sai token",
                                                                      "nav": "login"}, status_code=401)
        failures.pop(ip, None)
        exp = str(int(time.time()) + SESSION_TTL)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("ab_session", f"{exp}.{sign('admin:' + exp)}", httponly=True, samesite="strict",
                        secure=is_https(request), max_age=SESSION_TTL)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("ab_session")
        return resp

    @app.post("/webhooks/github")
    async def github_webhook(request: Request):
        body = await request.body()
        if not settings.webhook_secret:
            raise HTTPException(503, "webhook secret not configured")
        if not verify_signature(settings.webhook_secret, body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "bad signature")
        delivery = request.headers.get("x-github-delivery", "")
        etype = request.headers.get("x-github-event", "")
        if not delivery:
            raise HTTPException(400, "missing delivery id")
        with tx(conn):
            if not record_delivery(conn, delivery, etype):
                return {"status": "duplicate"}
            handle_github_event(etype, json.loads(body or b"{}"))
        return {"status": "accepted"}

    def handle_github_event(etype: str, p: dict) -> None:
        pr = p.get("pull_request") or {}
        head = (pr.get("head") or {}).get("ref", "")
        ms = row(conn, "SELECT * FROM milestones WHERE integration_branch=?", head) if head else None
        if ms is None:
            audit(conn, "github", "webhook.ignored", event=etype)
            return
        if etype == "pull_request_review":
            rv = p.get("review") or {}
            state = (rv.get("state") or "").lower()
            verdict = {"approved": "approve", "changes_requested": "changes_requested"}.get(state, "comment")
            # untrusted text: stored redacted, passed to the Tech Lead only inside an untrusted fence
            evq.emit(conn, "GITHUB_FEEDBACK", project_id=ms["project_id"], milestone_id=ms["id"],
                     payload={"verdict": verdict, "reviewer": (rv.get("user") or {}).get("login", "?"),
                              "body": redact(rv.get("body") or "")[:3000], "pr": pr.get("number")},
                     dedupe_key=f"gh-review:{rv.get('id')}")
        elif etype == "pull_request" and p.get("action") == "closed":
            merged = bool(pr.get("merged"))
            conn.execute("INSERT INTO project_context (project_id, kind, content, created_at) VALUES (?,?,?,?)",
                         (ms["project_id"], "verified_fact",
                          f"PR #{pr.get('number')} for {ms['key']} was {'merged' if merged else 'closed'} on GitHub.",
                          now_iso()))
            conn.execute("UPDATE project_context SET active=0 WHERE project_id=? AND kind='outstanding_pr' AND content LIKE ?",
                         (ms["project_id"], f"{ms['key']}:%"))
        audit(conn, "github", f"webhook.{etype}", project_id=ms["project_id"])

    # ------------------------------------------------------------ JSON API
    @app.get("/api/status")
    def api_status(actor: str = Depends(auth)):
        return engine.status()

    @app.post("/api/projects/{slug}/goals")
    def api_goal(slug: str, body: GoalIn, actor: str = Depends(auth)):
        try:
            mid = engine.submit_goal(slug, body.goal, body.milestone, actor=actor)
        except PolicyViolation as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"milestone_id": mid}

    @app.post("/api/projects/{slug}/merge")
    def api_merge(slug: str, body: MergeIn, actor: str = Depends(auth)):
        try:
            engine.request_merge(slug, body.source, body.target, actor)
        except PolicyViolation as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/projects/{slug}/{action}")
    def api_project_action(slug: str, action: str, actor: str = Depends(auth)):
        if action not in ("pause", "resume"):
            raise HTTPException(404)
        with tx(conn):
            conn.execute("UPDATE projects SET paused=? WHERE slug=?", (int(action == "pause"), slug))
            audit(conn, actor, f"project.{action}", slug=slug)
        return {"ok": True}

    @app.get("/api/tasks/{task_id}")
    def api_task(task_id: int, actor: str = Depends(auth)):
        t = row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if t is None:
            raise HTTPException(404)
        return task_bundle(task_id)

    @app.get("/api/tasks/{task_id}/files")
    def api_task_file(task_id: int, path: str, actor: str = Depends(auth)):
        t = row(conn, "SELECT worktree_path FROM tasks WHERE id=?", task_id)
        if t is None or not t["worktree_path"] or not Path(t["worktree_path"]).exists():
            raise HTTPException(404, "task has no live worktree")
        try:
            content = read_confined(t["worktree_path"], path)
        except PolicyViolation as e:
            with tx(conn):
                audit(conn, actor, "policy.path_denied", task_id=task_id, path=path[:200])
            raise HTTPException(403, str(e))
        except FileNotFoundError:
            raise HTTPException(404, "not found")
        return {"path": path, "content": redact(content)}

    @app.post("/api/tasks/{task_id}/cancel")
    def api_cancel(task_id: int, actor: str = Depends(auth)):
        try:
            engine.cancel_task(task_id, actor)
        except Exception as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.get("/api/agents")
    def api_agents(actor: str = Depends(auth)):
        return agents_view()

    @app.post("/api/agents/{agent_id}/{action}")
    async def api_agent_action(agent_id: str, action: str, request: Request, actor: str = Depends(auth)):
        if row(conn, "SELECT 1 FROM agents WHERE id=?", agent_id) is None:
            raise HTTPException(404)
        if action in ("pause", "resume"):
            quota.set_paused(conn, agent_id, action == "pause", actor)
        elif action in ("enable", "disable"):
            with tx(conn):
                conn.execute("UPDATE agents SET enabled=? WHERE id=?", (int(action == "enable"), agent_id))
                audit(conn, actor, f"agent.{action}", agent=agent_id)
        elif action == "concurrency":
            if "json" in request.headers.get("content-type", ""):
                value = (await request.json()).get("value")
            else:
                value = (await request.form()).get("value")
            n = int(value)
            if not 0 <= n <= 8:
                raise HTTPException(400, "concurrency must be 0..8")
            with tx(conn):
                conn.execute("UPDATE agents SET max_concurrency=? WHERE id=?", (n, agent_id))
                audit(conn, actor, "agent.concurrency", agent=agent_id, value=n)
        else:
            raise HTTPException(404)
        return {"ok": True}

    @app.get("/api/approvals")
    def api_approvals(actor: str = Depends(auth)):
        return q("SELECT * FROM approval_requests ORDER BY id DESC LIMIT 100")

    @app.post("/api/approvals/{approval_id}/{action}")
    async def api_approval(approval_id: int, action: str, request: Request, actor: str = Depends(auth)):
        if action not in ("approve", "reject"):
            raise HTTPException(404)
        note = ""
        if "form" in request.headers.get("content-type", ""):
            note = str((await request.form()).get("note", ""))
        try:
            engine.resolve_approval(approval_id, action == "approve", actor, note)
        except PolicyViolation as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.get("/api/stream")
    async def api_stream(request: Request, actor: str = Depends(auth)):
        async def gen():
            last = row(conn, "SELECT COALESCE(MAX(id),0) m FROM audit_log")["m"]
            while not await request.is_disconnected():
                new = rows(conn, "SELECT * FROM audit_log WHERE id > ? ORDER BY id LIMIT 100", last)
                for r in new:
                    last = r["id"]
                    yield f"data: {json.dumps({k: r[k] for k in r.keys()})}\n\n"
                if not new:
                    yield ": keepalive\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------ helpers
    def agents_view() -> list[dict]:
        out = []
        for a in q("SELECT * FROM agents ORDER BY id"):
            d, w, dr = quota.invocation_counts(conn, a["id"])
            u = row(conn, "SELECT COUNT(*) n, SUM(duration_s) dur, SUM(input_tokens) itok, SUM(output_tokens) otok, "
                          "SUM(outcome IN ('rate_limited','usage_limited')) lim FROM quota_usage WHERE agent_id=?", a["id"])
            cap = jload(a.pop("capability_json"), {})
            a.pop("config_json", None)  # may reference identity paths; never shown
            out.append(a | {"roles": jload(a.pop("roles_json"), []), "capability": cap,
                            "auth": cap.get("auth", {"state": "not checked"}),
                            "usage": {"last_24h": d, "last_7d": w, "reviews_24h": dr, "total": u["n"],
                                      "duration_s": round(u["dur"] or 0, 1), "input_tokens": u["itok"],
                                      "output_tokens": u["otok"], "limit_errors": u["lim"] or 0},
                            "running": quota.running_count(conn, a["id"])})
        return out

    def task_bundle(task_id: int) -> dict:
        t = dict(row(conn, "SELECT * FROM tasks WHERE id=?", task_id))
        arts = q("SELECT * FROM artifacts WHERE task_id=? ORDER BY id", task_id)

        def latest(kind: str, limit: int = 60_000) -> str:
            for a in reversed(arts):
                if a["kind"] == kind and Path(a["path"]).exists():
                    return redact(Path(a["path"]).read_text(errors="replace")[-limit:])
            return ""
        return {
            "task": t, "acceptance": jload(t["acceptance_json"], []), "allowed_paths": jload(t["allowed_paths_json"], []),
            "deps": q("SELECT x.id, x.key, x.state FROM task_dependencies d JOIN tasks x ON x.id=d.depends_on_id "
                      "WHERE d.task_id=?", task_id),
            "attempts": q("SELECT id, agent_id, purpose, status, outcome, exit_code, duration_s, started_at, ended_at, "
                          "failure_detail, input_tokens, output_tokens FROM execution_attempts WHERE task_id=? ORDER BY id",
                          task_id),
            "transitions": q("SELECT * FROM task_transitions WHERE task_id=? ORDER BY id", task_id),
            "validations": [v | {"results": jload(v["results_json"], []), "violations": jload(v["policy_violations_json"], []),
                                 "files": jload(v["changed_files_json"], [])}
                            for v in q("SELECT * FROM validation_runs WHERE task_id=? ORDER BY id DESC", task_id)],
            "reviews": q("SELECT * FROM reviews WHERE task_id=? ORDER BY id", task_id),
            "prompt": latest("prompt", 20_000), "stdout": latest("stdout", 20_000), "stderr": latest("stderr", 8_000),
            "diff": latest("diff", 60_000),
        }

    def dep_graph_svg(milestone_id: int) -> str:
        tasks = q("SELECT id, key, state FROM tasks WHERE milestone_id=? ORDER BY id", milestone_id)
        deps = q("SELECT d.task_id, d.depends_on_id FROM task_dependencies d JOIN tasks t ON t.id=d.task_id "
                 "WHERE t.milestone_id=?", milestone_id)
        if not tasks:
            return ""
        parents: dict[int, list[int]] = {t["id"]: [] for t in tasks}
        for d in deps:
            parents[d["task_id"]].append(d["depends_on_id"])
        level: dict[int, int] = {}

        def lv(i, seen=()):
            if i in level:
                return level[i]
            if i in seen:
                return 0
            level[i] = 1 + max([lv(p, seen + (i,)) for p in parents.get(i, []) if p in parents] or [-1])
            return level[i]
        for t in tasks:
            lv(t["id"])
        cols: dict[int, list[dict]] = {}
        for t in tasks:
            cols.setdefault(level[t["id"]], []).append(t)
        W, H, gx, gy = 168, 44, 56, 18
        pos = {}
        for c, ts in cols.items():
            for r, t in enumerate(ts):
                pos[t["id"]] = (12 + c * (W + gx), 12 + r * (H + gy))
        width = 24 + (max(cols) + 1) * (W + gx)
        height = 24 + max(len(v) for v in cols.values()) * (H + gy)
        parts = [f'<svg class="graph" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
                 f'aria-label="Task dependency graph">']
        for d in deps:
            if d["task_id"] in pos and d["depends_on_id"] in pos:
                x1, y1 = pos[d["depends_on_id"]]
                x2, y2 = pos[d["task_id"]]
                parts.append(f'<path class="edge" d="M{x1 + W},{y1 + H / 2} C{x1 + W + gx / 2},{y1 + H / 2} '
                             f'{x2 - gx / 2},{y2 + H / 2} {x2},{y2 + H / 2}"/>')
        from html import escape
        for t in tasks:
            x, y = pos[t["id"]]
            parts.append(f'<a href="/tasks/{t["id"]}"><rect class="node st-{t["state"]}" x="{x}" y="{y}" width="{W}" '
                         f'height="{H}" rx="6"/><text x="{x + 10}" y="{y + 18}" class="nk">{escape(t["key"][:22])}</text>'
                         f'<text x="{x + 10}" y="{y + 35}" class="ns">{t["state"]}</text></a>')
        parts.append("</svg>")
        return "".join(parts)

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, actor: str = Depends(auth)):
        return page(request, "overview.html", cards=views.progress_cards(conn), groups=views.quota_groups(conn),
                    activity=views.activity(conn), counts=views.counters(conn))

    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request, actor: str = Depends(auth)):
        return page(request, "projects.html", projects=q(
            "SELECT p.*, (SELECT COUNT(*) FROM milestones m WHERE m.project_id=p.id) nm, "
            "(SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.state NOT IN ('COMPLETED','CANCELLED','FAILED')) open "
            "FROM projects p ORDER BY p.id"))

    @app.get("/projects/{slug}", response_class=HTMLResponse)
    def project_page(request: Request, slug: str, actor: str = Depends(auth)):
        p = row(conn, "SELECT * FROM projects WHERE slug=?", slug)
        if p is None:
            raise HTTPException(404)
        ms = q("SELECT * FROM milestones WHERE project_id=? ORDER BY id DESC", p["id"])
        for m_ in ms:
            m_["tasks"] = q("SELECT * FROM tasks WHERE milestone_id=? ORDER BY id", m_["id"])
            m_["progress"] = views.milestone_progress(conn, m_["id"])
            m_["graph"] = dep_graph_svg(m_["id"])
        manifest = jload(p["manifest_json"], {})
        return page(request, "project.html", p=dict(p), milestones=ms, manifest=manifest,
                    context=q("SELECT * FROM project_context WHERE project_id=? AND active=1 ORDER BY id DESC LIMIT 40",
                              p["id"]))

    @app.post("/projects/{slug}/goal")
    async def project_goal_form(request: Request, slug: str, actor: str = Depends(auth)):
        form = await request.form()
        try:
            engine.submit_goal(slug, str(form.get("goal", "")), str(form.get("milestone") or "") or None, actor=actor)
        except (PolicyViolation, ValueError) as e:
            raise HTTPException(403, str(e))
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.post("/projects/{slug}/toggle")
    def project_toggle(slug: str, actor: str = Depends(auth)):
        with tx(conn):
            conn.execute("UPDATE projects SET paused=1-paused WHERE slug=?", (slug,))
            audit(conn, actor, "project.toggle_pause", slug=slug)
        return RedirectResponse(f"/projects/{slug}", status_code=303)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_page(request: Request, task_id: int, actor: str = Depends(auth)):
        if row(conn, "SELECT 1 FROM tasks WHERE id=?", task_id) is None:
            raise HTTPException(404)
        return page(request, "task.html", b=task_bundle(task_id))

    @app.post("/tasks/{task_id}/cancel")
    def task_cancel_form(task_id: int, actor: str = Depends(auth)):
        try:
            engine.cancel_task(task_id, actor)
        except Exception as e:
            raise HTTPException(409, str(e))
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request, actor: str = Depends(auth)):
        return page(request, "agents.html", agents=agents_view(), groups=views.quota_groups(conn))

    @app.post("/agents/{agent_id}/{action}")
    async def agent_form(request: Request, agent_id: str, action: str, actor: str = Depends(auth)):
        await api_agent_action(agent_id, action, request, actor)
        return RedirectResponse("/agents", status_code=303)

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, actor: str = Depends(auth)):
        aps = q("SELECT a.*, p.slug FROM approval_requests a JOIN projects p ON p.id=a.project_id ORDER BY "
                "a.status='PENDING' DESC, a.id DESC LIMIT 100")
        for a in aps:
            a["evidence"] = jload(a["evidence_json"], {})
        trail = q("SELECT * FROM audit_log WHERE action LIKE 'approval.%' ORDER BY id DESC LIMIT 50")
        return page(request, "approvals.html", approvals=aps, trail=trail)

    @app.post("/approvals/{approval_id}/{action}")
    async def approval_form(request: Request, approval_id: int, action: str, actor: str = Depends(auth)):
        await api_approval(approval_id, action, request, actor)
        return RedirectResponse("/approvals", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, actor: str = Depends(auth)):
        gh_ok, gh_msg = engine.github.available() if settings.github.get("enabled") else (False, "disabled")
        return page(request, "settings.html", s=settings, gh_ok=gh_ok, gh_msg=gh_msg,
                    projects=[{"slug": p["slug"], "manifest": jload(p["manifest_json"], {})}
                              for p in rows(conn, "SELECT slug, manifest_json FROM projects")],
                    webhook_configured=bool(settings.webhook_secret))

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):
        if exc.status_code == 307 and exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app
