# AgentBridge

A self-hosted, event-driven orchestrator that lets several AI coding CLIs work together on a
software project: a Tech Lead agent plans and reviews, worker agents implement in isolated Git
worktrees, and AgentBridge enforces validation, quotas and safety policy in between.

```
owner goal ─▶ Tech Lead (structured JSON decisions, schema + policy checked)
                 │ CREATE_TASKS
                 ▼
          task queue (SQLite) ──▶ worker CLI in its own git worktree
                 ▲                       │ result + commits
                 │                       ▼
   Tech Lead ◀── events ◀── deterministic validation (allowlisted commands, path policy)
   (approve / revise / review / complete)        └─▶ automatic bounded retry on failure
                 │ APPROVE
                 ▼
   merge into agentbridge/<project>/<milestone>/integration ──▶ draft PR (owner approval)
```

Status: **v0.1 MVP**. Phases 1–3 are implemented and tested. Phase 4 deployment files are
written but have **not** been run on the Ubuntu N100 (no access from the build environment).
See [docs/STATUS.md](docs/STATUS.md) for the exact implemented vs. unsupported list.

## Quick start (mock providers, no quota used)

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/agentbridge demo                 # synthetic repo, mock Tech Lead + 2 workers + reviewer
.venv/bin/pytest                           # 42 tests, real subprocesses and git, no provider calls
```

The demo submits *"Fix add() and implement mul() in calc.py with tests"*. You will see the Tech
Lead split it into two dependent tasks, request a revision, a buggy first attempt fail
`unittest` and get retried automatically, an independent review, integration and milestone completion.

Dashboard on the demo data:

```bash
AGENTBRIDGE_ADMIN_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))') \
  .venv/bin/agentbridge serve          # http://127.0.0.1:8787, sign in with the token
```

## Using real agents

1. Copy `examples/agentbridge.example.yaml` to `agentbridge.yaml` and adjust identities.
2. Log in each identity once on the host ([docs/AGENT_AUTH.md](docs/AGENT_AUTH.md)).
3. `agentbridge agents detect`: capability and login state for each identity, with no prompts sent.
4. `agentbridge agents smoke <id>`: one minimal schema-constrained call (uses one invocation).
5. `agentbridge project add examples/<project>.yaml`, then `agentbridge goal <slug> "<goal>"`.
6. `agentbridge run` (the host engine; under systemd in production).

## CLI

| Command | Purpose |
|---|---|
| `init` | create/migrate DB, sync agent identities from config |
| `project add/list/pause/resume` | manage projects (YAML manifests) |
| `goal <project> "<text>" [--milestone KEY]` | submit an engineering goal |
| `run [--until-idle] [--max-seconds N]` | host engine loop (resumable) |
| `serve [--dev]` | API + dashboard (no agent execution) |
| `status`, `tasks`, `events -n N` | inspect state / audit log |
| `agents list/detect/smoke/enable/disable/pause/resume/concurrency` | agent management |
| `approvals list/approve/reject <id>` | human approvals |
| `backup <file>`, `restore <file> --force`, `recover` | operations |
| `demo` | synthetic end-to-end demo with mock agents |

## Layout

```
agentbridge/
  adapters/       AgentAdapter interface + claude, antigravity, codex, gemini, mindlogic, mock
  orchestrator/   engine (event loop, dispatch, recovery), states, decisions, quota, context, events
  workers/        crash-safe process wrapper, git worktrees, deterministic validation
  policies/       safety policy (paths, commands, branches, hardware no-go, redaction), manifest schema
  integrations/   GitHub (gh CLI draft PRs, webhook verification)
  dashboard/      server-rendered Jinja templates
  migrations/     SQL schema migrations
  api.py, cli.py, demo.py, config.py, db.py
tests/            acceptance + unit tests
deployment/       install.sh, systemd units, healthcheck
examples/         config, demo manifest, G3X Lite manifest
docs/             architecture, deployment, auth, capability matrix, security, status
```

The suggested `backend/` top-level folder is the `agentbridge/` Python package. Migrations live
inside the package so they ship with `pip install`.

## Documentation

- [Architecture](docs/ARCHITECTURE.md): control plane vs host engine, state machine, recovery
- [Deployment on Ubuntu N100](docs/DEPLOYMENT.md): install, Docker, systemd, Tailscale, backup/restore
- [Agent authentication](docs/AGENT_AUTH.md): separate Claude identities, Antigravity, Codex, Gemini, Mindlogic
- [Provider capability matrix](docs/CAPABILITY_MATRIX.md): what was verified on a real host
- [Work allocation across quota pools](docs/ALLOCATION.md): which agent does what, rolling limits
- [Security policy](docs/SECURITY.md)
- [Implemented vs unsupported](docs/STATUS.md)
