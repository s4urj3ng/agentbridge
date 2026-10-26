# Provider capability matrix

Verified on the development host (macOS 14, 2026-10-27) with real invocations unless marked
otherwise. Re-check on the deployment host with `agentbridge agents detect` and
`agentbridge agents smoke <id>`.

| Capability | Claude Code 2.1.310 | Antigravity `agy` 1.4.7 | Codex CLI | Gemini CLI | Mindlogic | Mock |
|---|---|---|---|---|---|---|
| Installed on dev host | yes | yes | **no** | **no** | n/a (HTTP) | yes |
| Headless mode | `-p` ✅ verified | `--print` ✅ verified | `exec` (documented, unverified) | `-p` (documented, unverified) | HTTP | ✅ |
| Structured output | `--json-schema` → `structured_output` ✅ | `--json-schema` → `structured_output` ✅ | `--output-schema` (unverified) | prompt + parse (unverified) | prompt + parse (unverified) | ✅ |
| Token usage reported | ✅ `usage` (+ cache tokens, `total_cost_usd`) | ✅ `usage.input_tokens/output_tokens` | NDJSON `usage` if emitted (unverified) | no | `usage` if API returns it | none |
| Separate identities | ✅ `CLAUDE_CONFIG_DIR` (fresh dir = logged out) | ❌ none found; single host identity | `CODEX_HOME` (unverified) | ❌ not assumed | per API key | n/a |
| Non-interactive auth check | ✅ `claude auth status --json` | ✅ `agy models` (no prompt sent) | `codex login status` (unverified) | ❌ | key presence only | n/a |
| Tool restriction | ✅ `--tools ""`, `--allowedTools`, `--permission-prompts none` | `--mode plan / accept-edits`, `--sandbox` | `--sandbox read-only / workspace-write` | `--approval-mode` | no tools | n/a |
| File editing in worktree | yes (acceptEdits + allowlist) | ✅ verified (fixed `calc.py`) | expected | expected | ❌ text only | ✅ |
| Roles in AgentBridge | tech_lead, worker, reviewer | worker, reviewer | worker, reviewer | worker | reviewer | all |
| End-to-end loop verified | ✅ as Tech Lead (haiku) | ✅ as worker | ❌ | ❌ | ❌ | ✅ (all tests) |

## Real verification log (2026-10-27)

1. `claude -p … --json-schema` minimal call: `structured_output={"status":"ok","n":7}`.
2. `agy --print … --json-schema` minimal call: `structured_output={"n":7,"status":"ok"}`.
3. `agentbridge agents smoke claude-host`: ok (3,952 in / 74 out tokens, 3.9 s).
4. `agentbridge agents smoke agy-worker`: ok (19,175 in / 26 out tokens, 12.3 s).
5. Real loop on a synthetic repo, Claude (haiku) as Tech Lead and Antigravity as worker:
   `CREATE_TASKS` → worker fixed `add()` in its worktree → `unittest` passed → `APPROVE_TASK`
   → merged into the integration branch → `COMPLETE_MILESTONE`. Four invocations total; `main`
   untouched.
6. Bug found by the real run and fixed: Claude Code's `--json-schema` is strict and rejects
   Pydantic's `discriminator` keyword. Schemas are now exported without it
   (`decisions.portable_schema`), with a regression test.
7. `CLAUDE_CONFIG_DIR=<fresh dir> claude auth status --json` → `loggedIn: false`; default dir
   → `loggedIn: true, authMethod: claude.ai`.

Not verified: the two specific accounts (Claude Team Standard / Claude Personal). Only the
mechanism was verified; the logins must be done on the server.
