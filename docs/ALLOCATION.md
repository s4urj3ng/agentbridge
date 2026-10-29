# Work allocation across quota pools

Owner's quota picture (2026-10-27) and the resulting setup:

| Pool | Window | AgentBridge identity | Model | Role |
|---|---|---|---|---|
| Claude Team | 5 h + weekly | `claude-team-lead` | Opus 5.5, effort medium | Tech Lead |
| Claude Pro | 5 h + weekly | `claude-personal-worker` | Opus 5.5, effort medium | hard coding tasks |
| Antigravity, Gemini 3.8 Flash | 5 h + weekly (abundant) | `antigravity-worker` | `gemini-3.8-flash-high` | **default worker** |
| Antigravity, Gemini 4 Argon | weekly | `antigravity-argon` | `gemini-4-argon-high` | worker when Flash is not enough; fallback reviewer |
| Antigravity, external Claude | 5 h + weekly, **small** | `antigravity-claude-reviewer` | `claude-opus-5-5-medium` | **preferred reviewer** of run results |
| ChatGPT / Codex | 5 h + weekly | `codex-reviewer` | `gpt-6.1-sol`, reasoning high | high-risk second opinion; last fallback reviewer |
| Mindlogic | credits | disabled | | not used for now |

## Flow

```
goal ─▶ Tech Lead (Claude Team, Opus 5.5 medium) plans tasks
          │
          ▼  default
   antigravity-worker (Gemini 3.8 Flash high) ──quota out──▶ antigravity-argon (Gemini 4 Argon)
          │   hard / risky / failed-on-Gemini tasks ─────────▶ claude-personal-worker (Opus 5.5 medium)
          ▼                                                          └─quota out─▶ antigravity-argon
   deterministic validation (+ automatic retries, no LLM call)
          │
          ▼  review (when the Tech Lead asks for one)
   antigravity-claude-reviewer (Claude in Antigravity) ──5 h window full──▶ antigravity-argon ──▶ codex-reviewer
          │
          ▼
   Tech Lead approves / requests revision ─▶ integration branch
```

## Mechanisms

- **`fallback_to`**: if an agent is refused for quota reasons (provider usage/rate-limit cooldown,
  rolling window full, budget reached, circuit open), the task or review goes to the first
  fallback that is admitted **and** allowed by the project manifest in that role. A reviewer
  fallback is never the task's own author. Paused or disabled agents do not fall back: they wait
  for the owner. Every reroute is in the audit log (`task.fallback`, `review.fallback`).
  A Telegram role-order override replaces it with the project's list order (see below).
- **`rolling_limits`** caps calls per window, for example `[{hours: 5, max: 6}, {hours: 168, max: 40}]`
  on the Antigravity-Claude reviewer. When its 5-hour cap is reached, reviews move on
  immediately instead of waiting for the next window or draining the weekly allowance.
  `reserve_for_review` can hold part of a window back for reviews.
- **Real provider numbers where available:** Claude Code reports its 5-hour and 7-day utilisation on
  every call. AgentBridge stores the report and refuses the agent, which triggers the fallback,
  when a window is `rejected`, or when it reaches `provider_stop_pct` (for example
  `{five_hour: 95, seven_day: 85}`). That way work moves elsewhere before the weekly pool is
  drained. Antigravity's `/usage` command reports its real quota for free (no model turn), per
  group: Gemini (Flash/Pro), Claude and GPT, and Argon. The engine refreshes it every
  `quota_poll_s` (10 min), and the Telegram bot refreshes it when you open the quota view, so the
  same stop-level/fallback logic applies to the three Antigravity identities. For example, the
  Claude-in-Antigravity reviewer hands reviews to Argon or Codex once its real 5-hour bucket
  passes 90%. Codex's numbers come from its own session files after every run, so `codex-reviewer`
  can use `provider_stop_pct` as well.
- **Provider errors win:** a real usage-limit or rate-limit error puts that identity into cooldown
  (using the reset or retry-after time when the CLI reports one) and triggers the same fallback.
- **Profiles** (`profile:` text) are shown to the Tech Lead so it picks the right pool when it
  creates tasks and requests reviews. The manifest's `reviewers` list is ordered: first is
  preferred.
- **Effort:** Claude `effort: medium` → `--effort medium` (verified flag). Codex
  `reasoning_effort: high` → `-c model_reasoning_effort=high`. Antigravity encodes effort in the
  model id (`…-high`, `…-medium`).
- **Antigravity external models** (`claude-*`, `gpt-oss*`) are only allowed on identities with
  `allow_external_models: true`, so the small pool is used deliberately, never by accident.

## Changing assignment from Telegram (runtime overrides)

With `telegram.allow_config_changes: true`, the **⚙️ Phân công** screen ([TELEGRAM.md](TELEGRAM.md))
keeps **one ordered list per role** for each project:

- **CODE** (workers): item 1 is the default worker, the rest are its fallbacks in order;
- **REVIEW** (reviewers): item 1 is the preferred reviewer.

**Fallback with an override:** when a project has a role-order override, `Engine._pick_agent` ignores
the agents' `fallback_to` for that role. A quota-refused agent hands the work to the next admitted
agent below it in the list, then wraps round to the ones above. Reviews skip the task's author.

**Without an override:** the old behaviour applies (manifest order plus each agent's `fallback_to`
from the yaml).

Per agent, the screen also sets **enabled/disabled** and **`max_concurrency`** (0–8). Per-agent
`fallback_to` overrides from the first Telegram version are still honoured, but can no longer be
edited. Legacy `default_worker` overrides are read as "move this agent to the top of CODE".

Overrides live in the `overrides` table (migration `0003_overrides`). Every change keeps a snapshot
in `override_history` (`0004_override_history`) for **↩️ Hoàn tác**. They take precedence over
`/etc/agentbridge/agentbridge.yaml` and the project manifest and survive `sync_agents()` and
restarts. **♻️ Về gốc** deletes them and restores the config values. Where they are used:

- `Engine.manifest()` applies project overrides, so the Tech Lead roster (`_agent_roster`, the
  "Available identities" prompt section, which states that lists are in preference order),
  decision validation (`CREATE_TASKS`, `ASSIGN_TASK`, `REQUEST_REVIEW`), dispatch and review
  routing all see the new order.
- `enabled` / `max_concurrency` are written to the `agents` row and re-applied after every config sync.
- A queued task whose assignee was removed from CODE goes to the first admitted agent in the new
  list. A pending review whose reviewer was removed goes to the first admitted reviewer that is not
  the task's author. These show as `task.reroute` / `review.reroute` in the audit log. Running work
  is not interrupted. Disabling an agent works as before: its queued tasks wait and are not rerouted.

Validation refuses unknown ids, wrong roles, adding a disabled agent, duplicates, removing the last
agent of a role, and a REVIEW list that contains only CODE item 1 (a task's author never reviews
it). Hard safety policy is not configurable here: hardware no-go, protected branches and the
Antigravity external-model guard apply unchanged. Every change is audited (`config.override` /
`config.reset` / `config.undo`, actor `telegram:<user id>`).

## Unattended projects (`policy.autonomous`)

With `policy.autonomous: true` (and `auto_start: true`) in a project manifest, the owner
pre-authorises the Tech Lead to keep a milestone moving without asking. A failed or blocked task is
revised with concrete feedback or reassigned to the next listed worker. Passing work is reviewed by
the first listed reviewer, and a "changes requested" review leads to a revision. The prompt carries
these rules, and AgentBridge rejects any `REQUEST_HUMAN_APPROVAL` except `scope_change`.

Limits still bound the loop (`max_revisions`, `max_execution_failures`,
`max_tl_invocations_per_milestone`). When a cap is hit, the engine itself asks the owner. Draft PRs
still need approval, and hard policy is unchanged. G3X Lite runs this way since 2026-10-28, with
`review_policy: always`.

Owner commands:

```bash
agentbridge milestone cancel PORT-9B --project g3x-lite --reason "done elsewhere"
agentbridge goal g3x-lite - --milestone PORT-10 < handoff.md
```

`milestone cancel` cancels open tasks (running workers are stopped), expires pending approvals and
drops queued events. `goal -` reads a long goal or hand-over text from stdin.

## Antigravity tool permissions

Headless `agy` auto-denies tools it cannot prompt for. A worker that needs to run commands (tests,
git) must have them allowed in the `agentbridge` user's `~/.gemini/antigravity-cli/settings.json`,
for example `{"permissions": {"allow": ["command(python3)", "command(git status)"]}}`. Without
that, the adapter reports "denied tool permission(s)" instead of an invalid result.

On a host you treat as a sandbox, set `skip_permissions: true` on the Antigravity identity instead:
AgentBridge then passes `--dangerously-skip-permissions`, so no command is denied and the allow list
is not used. The agent then has everything the `agentbridge` user has, including the GitHub
credentials and the other agents' logins; only the prompt tells it not to commit, push or merge.

## Tuning

All `max` values are starting points; the providers' real quotas are not machine-readable. Watch
**Agents → Usage** (calls per 5 h / 7 d, limit errors) for a few days and set each cap a little
below where real limit errors begin. The Codex model id `gpt-6.1-sol` was read from `/model` in
`codex` on the server; `agents smoke codex-reviewer` fails clearly if it is wrong.
