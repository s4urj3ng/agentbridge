# Work allocation across quota pools

Four subscription agents, each with different limits (as described by the owner, 2026-10-27):

| Pool | Window | Scarcity | AgentBridge identity | Role |
|---|---|---|---|---|
| Claude Team | 5 h + weekly | scarce | `claude-team-lead` | Tech Lead only |
| Claude Pro | 5 h + weekly | scarce | `claude-personal-worker` | hard coding tasks |
| ChatGPT / Codex | 5 h + weekly | scarce | `codex-reviewer` | second-opinion review, high-risk only |
| Antigravity Gemini 3.8 Flash | 5 h + weekly | **abundant** | `antigravity-worker` | **default worker**, also reviewer |
| Antigravity Gemini 4 Argon | weekly | medium | `antigravity-argon` | preferred reviewer, backup worker |
| Antigravity external models (Claude, GPT-OSS) | separate, tiny | very scarce | none | **blocked** by the adapter |
| Mindlogic | credits | n/a | disabled | not used for now |

## How the routing works

1. **Default path is cheap.** Each project's `default_worker` is `antigravity-worker` (Gemini 3.8
   Flash). Deterministic validation runs after every attempt. Failed tests are retried
   automatically with the failure output (`max_validation_retries`), so cheap retries never cost a
   Tech Lead call.
2. **The Tech Lead sees owner-written profiles** (`profile:` in the config) for every worker and
   reviewer. It uses them to send only complex, risky or previously failed work to
   `claude-personal-worker`, and to request reviews from `antigravity-argon` (normal) or
   `codex-reviewer` (high-risk only). These are guidance. The hard limits below are enforced
   regardless of what the Tech Lead decides.
3. **The Tech Lead itself is frugal.** It is only invoked on events. `model: sonnet` handles routine
   events (task ready, review done, milestone ready); `event_models` switches to `opus` only for
   planning (`GOAL_SUBMITTED`) and unblocking (`TASK_BLOCKED`, `VALIDATION_EXHAUSTED`,
   `HUMAN_DECISION`). Its prompt is a compact state summary, not the project history.
4. **Rolling windows.** `rolling_limits: [{hours: 5, max: N}, {hours: 168, max: M}]` caps
   invocations per agent in each window. `reserve_for_review` keeps part of a window for reviews.
   An agent at its cap simply waits; other agents keep working.
5. **Real limit errors win.** When a CLI reports a usage or rate limit, that identity goes into
   cooldown (using the reset or retry-after time when the CLI reports one, otherwise exponential
   backoff) and its tasks are requeued, bounded by `max_quota_requeues`. If a scarce worker stays
   exhausted, the task is blocked and the Tech Lead can reassign it to another pool
   (`ASSIGN_TASK`).
6. **Antigravity external models are refused.** `model` must be set explicitly, and
   `claude-*` / `gpt-oss*` models are rejected unless `allow_external_models: true`. A task
   assigned to such an identity is blocked immediately instead of burning that quota.

## Tuning

The `max` numbers in `examples/agentbridge.example.yaml` are starting points, not the providers'
real quotas, which are not machine-readable. After a few days, compare **Agents → Usage** (calls
per 5 h / 7 d, limit errors) with when each provider actually ran out, and adjust. A rule of thumb:
set each scarce pool's cap a little below the point where real limit errors started.

Global concurrency (`limits.max_concurrent_agents`, default 3 here) bounds how many CLIs run at
once on the N100. The two Antigravity identities share one login, so keep their combined
`max_concurrency` modest.
