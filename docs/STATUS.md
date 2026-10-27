# Implemented vs. unsupported (v0.1.0)

## Phase 1: orchestration core ✅ implemented and tested

- SQLite (WAL) schema with migrations: projects, structured project context, milestones, tasks,
  dependencies, transitions, agents, execution attempts, artifacts, validation runs, reviews, git
  refs, worktree leases, decisions, quota usage, events, audit log, approvals, webhook deliveries.
- Task engine with all 12 states, enforced transition table, compare-and-set transitions,
  transaction-safe claiming, idempotent deduplicated event queue.
- Crash-safe detached execution wrapper; recovery that distinguishes finished, running and lost
  processes.
- `AgentAdapter` interface: `detect`, `validate_auth`, `prepare`/`execute`, `collect_result`,
  `classify_failure`, `estimate_usage`, `cancel`.
- Claude Code adapter (verified), isolated worktree runner, structured Tech Lead decisions,
  management CLI, automated tests.

## Phase 2: multi-agent orchestration ✅ implemented and tested (mock)

- Antigravity adapter (**verified** with real calls), Codex adapter (**verified** by smoke test on
  the N100, codex-cli 0.164.0), Gemini CLI adapter (implemented with flag detection;
  **unverified**: CLI not installed), Mindlogic HTTP adapter (implemented, disabled, **unverified**).
- Dependency scheduling, independent review routing (reviewer ≠ author, detached read-only
  worktree), review policy (`never` / `risk` paths / `always`).
- Quota controls: per-agent concurrency, global concurrency, daily/weekly invocation ceilings,
  reserved reviewer capacity, cooldown from retry-after or exponential backoff, circuit breaker,
  bounded requeues, auth-failure pause, manual pause/resume.
- Failure recovery and bounded retries everywhere (validation, execution, Tech Lead, quota).
- Multi-agent synthetic demo (`agentbridge demo`).

## Phase 3: GitHub and dashboard ✅ implemented / ⚠️ partially verified

- Draft PR creation through `gh pr create --draft` after owner approval (or `draft_pr: auto`),
  gated on integration-branch validation. **Not exercised against GitHub**: no push authorization
  was given, so this path is untested end-to-end.
- GitHub webhook endpoint with signature verification and replay protection (tested); review
  feedback intake → `GITHUB_FEEDBACK` Tech Lead event; PR closed/merged → project context.
- Server-rendered dashboard: overview (live SSE events), projects with dependency-graph SVG,
  task detail (spec, prompt, logs, diff, validation, reviews, attempts, state history), agents
  (detection, auth state, enable/disable, pause/resume, concurrency, usage, cooldown), approvals
  (evidence, approve/reject, audit trail), settings (read-only).
- Token auth, signed session cookie, CSRF, localhost bind.

## Phase 4: deployment ⚠️ files written, not deployed

- Dockerfile + Compose (control plane), systemd units (engine, API, backup timer),
  `install.sh`, health check script, backup/restore commands (implemented and used locally),
  deployment docs.
- **Not done:** running them on the Ubuntu N100 (no access), building the Docker image (Docker
  daemon not running on the dev machine), executing the suite on Python 3.12 (static analysis
  says the code needs ≥ 3.11; tests ran on 3.14).

## Known limitations / unsupported in v1

- No OS-level sandbox per agent identity beyond the shared `agentbridge` user and the CLIs' own
  permission systems (see SECURITY.md).
- Antigravity runs as a single identity (no verified per-identity config).
- Prompts for `agy`/Gemini go on the command line (≤100 KB, truncated beyond).
- Quota "remaining" is never estimated, by design: only measured counts and owner ceilings.
- No automatic merges anywhere; no rebasing of stacked branches (squash-merges are detected
  instead).
- Settings are edited in files, not in the dashboard.
- Hardware no-go detection is keyword/regex-based, so it can produce false positives
  (deny-by-default) and is not a substitute for keeping hardware off the host.
- One engine per data directory; no horizontal scaling.
- Dashboard dependency graph is a simple layered SVG.
