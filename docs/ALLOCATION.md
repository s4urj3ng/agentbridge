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
- **`rolling_limits`** caps calls per window, for example `[{hours: 5, max: 6}, {hours: 168, max: 40}]`
  on the Antigravity-Claude reviewer. When its 5-hour cap is reached, reviews move on
  immediately instead of waiting for the next window or draining the weekly allowance.
  `reserve_for_review` can hold part of a window back for reviews.
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

## Tuning

All `max` values are starting points; the providers' real quotas are not machine-readable. Watch
**Agents → Usage** (calls per 5 h / 7 d, limit errors) for a few days and set each cap a little
below where real limit errors begin. The Codex model id `gpt-6.1-sol` was read from `/model` in
`codex` on the server; `agents smoke codex-reviewer` fails clearly if it is wrong.
