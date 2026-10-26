# Security policy

## Trust model

| Input | Trust |
|---|---|
| Owner (CLI, authenticated API/dashboard), `agentbridge.yaml`, project manifests | trusted |
| Tech Lead output | **untrusted proposal**: schema + policy validated before use |
| Worker stdout, summaries, diffs, source comments, repo documents, PR text, review comments, webhook payloads | **untrusted data** |

Untrusted text that reaches a prompt is redacted, truncated and wrapped in
`<<<UNTRUSTED_DATA …>>>` fences; fence markers inside the text are neutralised. More
importantly, prompt injection cannot gain privileges, because no decision type can run commands,
change policy, push, or merge into protected branches.

## Controls

| Control | Where | Tested |
|---|---|---|
| Strict decision schema (unknown types/fields rejected, bounded sizes) | `orchestrator/decisions.py` | `test_malformed_decisions_are_rejected_by_schema` |
| Policy checks per decision: assignee must be an enabled worker of the project, reviewer ≠ author, task in the event's milestone, depth/count/revision caps, acyclic deps, known validation names, hardware no-go | `engine._apply_one` | `test_malicious_tech_lead_cannot_bypass_policy` |
| Approvals need passing validation; `review_policy` can require an independent review | `engine._apply_one` | `test_approve_requires_passing_validation_and_review_policy` |
| Bounded Tech Lead retries, then escalation to a human; per-milestone invocation cap | `engine._tl_failed` | `test_malformed_tech_lead_output_is_bounded_and_escalated` |
| Validation = named manifest commands, argv lists, executable allowlist, no fetch-and-exec | `policies/safety.check_validation_command` | `test_validation_command_policy` |
| Path confinement (absolute, `..`, symlinks, `.git`) for orchestrator file APIs | `policies/safety.confine_path` | `test_file_api_cannot_escape_worktree` |
| Post-run change policy: forbidden paths, task path scope, symlink escapes, history rewrite, change count | `git_ops.unexpected_changes` | `test_forbidden_paths_block_task` |
| Protected branches: never created, written, pushed or merged into; only `agentbridge/*` is writable; merge endpoint always refuses | `safety.assert_agent_writable_branch`, `engine.request_merge` | `test_protected_branch_merge_blocked` |
| No destructive git: no reset/clean/force-push/rebase; dirty worktrees never removed; conflicts aborted | `workers/git_ops.py` | `test_git.py` |
| Timeouts with process-group kill | `workers/wrapper.py`, `process.run_cmd` | recovery tests |
| Environment allowlist for subprocesses (no `GITHUB_TOKEN`, API keys or admin token passed to agents) | `safety.scrubbed_env` | `test_claude_invocation_shapes` |
| Secret redaction of logs, artifacts, audit entries, prompts | `safety.redact` | `test_redaction` |
| Dashboard/API auth (bearer token or signed HttpOnly SameSite=Strict cookie), CSRF on cookie POSTs | `api.py` | `test_api_requires_auth_and_csrf` |
| GitHub webhook HMAC-SHA256 + delivery-ID replay protection | `integrations/github.py` | `test_webhook_signature_and_replay` |
| Hardware STRICT NO-GO (goal, task creation and dispatch time) | `safety.hardware_no_go_hits` | `test_strict_no_go_rejects_goal`, malicious TL test |
| Audit log of transitions, decisions (incl. rejected), approvals, policy denials | `audit_log` | throughout |

## Execution sandboxing: what is and is not enforced

- Agents run as the unprivileged `agentbridge` OS user under systemd hardening
  (`NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome=read-only`, `PrivateTmp`, restricted
  `ReadWritePaths`).
- **AgentBridge does not sandbox the agent CLI's own tools.** What an agent may do inside its
  worktree is governed by the CLI's own permission system (Claude `--allowedTools`/permission
  mode, Antigravity `--mode`/`--sandbox`, Codex `--sandbox`). AgentBridge detects out-of-scope
  results afterwards (forbidden paths, scope, symlinks, history rewrite) and blocks the task. A
  stronger boundary (one OS user per identity, bubblewrap/firejail, or a VM) is future work.
- Hardware no-go is enforced by text policy on goals and tasks, plus forbidden file paths and
  validation commands that only the owner defines. An agent CLI with shell access on a host that
  has a camera or SD card attached could still reach it. **Do not attach camera hardware or SD
  media to the AgentBridge host.**

## Credentials

Provider logins live only in the `agentbridge` user's identity directories, outside any
repository and outside the database. `agentbridge backup` exports only the SQLite database.
Secrets for AgentBridge itself (`AGENTBRIDGE_ADMIN_TOKEN`, `AGENTBRIDGE_SESSION_SECRET`,
`GITHUB_WEBHOOK_SECRET`, `MINDLOGIC_API_KEY`) come from `/etc/agentbridge/agentbridge.env`
(mode 640).

## Network exposure

The API binds to `127.0.0.1:8787`. Remote access goes through Tailscale (see DEPLOYMENT.md),
never a public port. `serve` refuses to start without an admin token, except `--dev` on
localhost. If the GitHub webhook is used, expose only `/webhooks/github` (for example via
Tailscale Funnel, path-restricted) and configure `GITHUB_WEBHOOK_SECRET`.
