# Architecture

## Two processes, one database

| | Control plane (`agentbridge serve`) | Host engine (`agentbridge run`) |
|---|---|---|
| Runs | Docker container **or** systemd on the host | systemd on the host, as user `agentbridge` |
| Does | API, dashboard, SSE, GitHub webhooks, owner intents (goals, approvals, pause) | claims events, invokes the Tech Lead, dispatches workers, validates, integrates, recovers |
| Executes agents | **never** | yes, as subprocesses in git worktrees |
| Needs provider logins | no | yes: CLIs keep auth in the agentbridge user's home / identity dirs |

Why the split: subscription CLIs (Claude Code, Antigravity) authenticate with host-local state
(config dirs, OS keychain or credential files). Copying that into a container is fragile and
risks leaking credentials, so agent execution stays on the host. The control plane holds no
provider credentials and can be containerised and restarted freely.

They share one SQLite database (WAL mode) in `/var/lib/agentbridge`. On Linux, a bind mount
shares the same kernel and filesystem, so WAL locking works between container and host. The
container must run with the host user's UID/GID (`AB_UID`/`AB_GID`). This does **not** hold
for Docker Desktop on macOS (VM filesystem); there, run both processes on the host.

Only one engine may run per data directory (`engine.lock`, `flock`).

## Event flow

Events (`events` table) are durable and deduplicated (`dedupe_key UNIQUE`). The Tech Lead is
invoked **only** for:

`GOAL_SUBMITTED`, `TASK_READY_FOR_REVIEW` (validation passed), `TASK_BLOCKED`,
`VALIDATION_EXHAUSTED`, `REVIEW_COMPLETED`, `MILESTONE_READY`, `HUMAN_DECISION`, `GITHUB_FEEDBACK`.

Everything else is deterministic: dependency promotion, dispatch, validation, automatic retries
after validation failure (bounded by `max_validation_retries`), quota cooldowns and integration.
Events of one project are processed in order, one Tech Lead call at a time per project.
Projects are independent.

## Task state machine

```
CREATED ─▶ QUEUED ─▶ DISPATCHED ─▶ RUNNING ─▶ VALIDATING ─▶ REVIEW_PENDING ─▶ APPROVED ─▶ COMPLETED
              ▲          │            │  │          │   │            │   │
              │          └─▶ QUEUED   │  │          │   └▶ REVISION_REQUIRED ─▶ QUEUED
              │                       │  │          └▶ BLOCKED (policy / exhausted)
              └── lost / quota requeue┘  └▶ BLOCKED / FAILED          + CANCELLED from any live state
```

`orchestrator/states.py` holds the full table. Every transition is a compare-and-set on
`(state, state_version)` inside `BEGIN IMMEDIATE`. That makes dispatch claiming
transaction-safe, and a stale writer gets `StaleTransition` instead of overwriting.
`RUNNING` can never go directly to `APPROVED` or `COMPLETED`.

## Crash-safe execution and recovery

Every agent invocation runs as
`python -m agentbridge.workers.wrapper <run_dir> <nonce> <timeout> -- <cli argv>` in a new
session. The wrapper feeds the prompt on stdin, writes stdout/stderr to files, kills the child's
process group on timeout, and atomically writes `exit.json` when the child ends. The attempt row
stores pid, nonce, run_dir and deadline **before** the engine waits.

On restart, `Engine.recover()`:

| Found | Meaning | Action |
|---|---|---|
| `exit.json` with matching nonce | process finished while engine was down | collect normally |
| wrapper alive, nonce in its command line | still running | re-adopt and wait |
| neither | lost (crash/reboot) | attempt `LOST`, task requeued (bounded) or `BLOCKED`; never completed |
| task `VALIDATING` / `APPROVED` | deterministic step interrupted | re-run validation / integration |
| event `PROCESSING` without live attempt | Tech Lead call lost | event back to `PENDING` |

The nonce check prevents PID reuse after reboot from being mistaken for a live process.

## Git model

- The source clone is only fetched; it is never checked out, reset or cleaned. User changes
  there are untouched (tested).
- Each milestone gets `agentbridge/<project>/<milestone>/integration`, created from the approved
  base (`<remote>/<base>` if a remote exists, else the local base branch).
- Each task gets its own branch and worktree from the integration head. A DB lease
  (`worktree_leases`) prevents two agents owning one writable worktree, and Git itself refuses a
  second checkout of the same branch.
- After a worker finishes, AgentBridge commits leftovers (generated caches excluded), computes
  the diff against the task base, checks policy (forbidden paths, task path scope, symlink
  escapes, history rewrite, change count), then runs validation.
- Approved tasks are merged with `--no-ff` into the integration branch inside its dedicated
  worktree. On conflict the merge is aborted and the task is blocked; nothing is reset.
- Reviewers get a detached, throwaway worktree at the task head.
- Squash-merged stacks: `is_integrated()` treats a branch as integrated if it is an ancestor
  **or** if `git merge-tree --write-tree target branch` yields the target's own tree. No rebases
  or force-pushes are needed.
- Draft PRs are `integration → base`, created only after owner approval (or `draft_pr: auto`)
  and only if the default validation passes on the integration branch. AgentBridge never merges.

## Context budget

The Tech Lead prompt contains: rules, charter, active structured project context (charter,
goals, constraints, verified facts, outstanding PRs), a one-line table of milestone tasks, the
event's task spec, the latest validation results (names, pass/fail, log paths), changed files,
a diff excerpt (≤12 KB, whole files smallest-first), the latest review, the last 6 decisions and
a machine-readable state block. Full logs and diffs are artifacts referenced by path. Worker
prompts contain only their task. Reviewer prompts contain the task, validation summary and diff.
Worker memory (sessions) is not shared with the Tech Lead: CLIs run with
`--no-session-persistence` where supported.
