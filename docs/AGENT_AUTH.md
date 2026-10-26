# Agent authentication setup

Rules:

- Log in **interactively, once, on the host, as the `agentbridge` user**, per identity.
- AgentBridge never copies tokens between identities, never stores them in the database or
  logs, and only reports login state (`ok` / `not_logged_in`) and method.
- Before automating any subscription-backed CLI, the owner must confirm that the provider's
  current terms and their account/plan permit this execution pattern (unattended, scripted
  invocations). AgentBridge does not bypass session or rate limits: it backs off on limit errors
  and enforces your configured invocation ceilings.

## Claude Code: two independent identities

Verified mechanism (Claude Code 2.1.310): `CLAUDE_CONFIG_DIR` selects the config/auth directory.
A fresh directory reported `loggedIn: false` while the default directory on the same machine was
logged in, so identities are independent. Each AgentBridge identity sets `config_dir`; the
adapter exports it as `CLAUDE_CONFIG_DIR` for that identity only.

```bash
sudo -iu agentbridge
mkdir -p ~/identities/claude-team-lead ~/identities/claude-personal-worker
chmod 700 ~/identities/*

# Tech Lead: sign in with the Claude Team (Standard seat) account
CLAUDE_CONFIG_DIR=~/identities/claude-team-lead claude auth login
CLAUDE_CONFIG_DIR=~/identities/claude-team-lead claude auth status --text

# Worker: sign in with the Claude Personal account
CLAUDE_CONFIG_DIR=~/identities/claude-personal-worker claude auth login
CLAUDE_CONFIG_DIR=~/identities/claude-personal-worker claude auth status --text
```

On a headless server `claude auth login` prints a URL to open on another device. On Linux the
credentials are stored inside each config directory. On macOS they go to the keychain, keyed
per config dir.

Invocation used by the adapter (verified flags):

- Tech Lead: `claude -p --output-format json --no-session-persistence --permission-prompts none --strict-mcp-config --tools "" --json-schema <schema>`
- Worker: same, plus `--permission-mode acceptEdits --allowedTools Read,Edit,Write,Glob,Grep,Bash(git status:*),…`
  (configurable per identity). `--dangerously-skip-permissions` is never used.
- Reviewer: `--tools Read,Glob,Grep` (read-only).

## Google Antigravity (`agy`)

Verified (agy 1.4.7): `agy --print <prompt> --output-format json --json-schema <schema>
--mode accept-edits|plan --model <id> [--sandbox]`. Login is the Antigravity app/CLI login of
the host user; `agy models` is used as a no-prompt auth probe.

No per-identity config directory was found in `agy --help`, so AgentBridge treats Antigravity as
a **single host identity**. The prompt is passed as an argument, so it is capped below Linux's
128 KiB per-argument limit.

```bash
sudo -iu agentbridge
agy            # complete the login flow once
agy models     # should list models
```

## OpenAI Codex CLI (unverified here)

The `codex` CLI was not installed on the development machine; the "Codex Web GPT" desktop app's
bundled binary is not the Codex CLI and is not used. To enable:

```bash
sudo -iu agentbridge
npm i -g @openai/codex
CODEX_HOME=~/identities/codex codex login      # ChatGPT sign-in
CODEX_HOME=~/identities/codex codex login status
agentbridge agents detect                      # checks `codex exec --help` for every flag used
```

The adapter uses `codex exec --json --skip-git-repo-check --sandbox read-only|workspace-write
--output-schema <file> --output-last-message <file> -`. If any flag is missing from the
installed version, `detect()` reports it and the adapter stays disabled. Keep Codex's
`daily_invocation_limit` low and use it as an explicitly requested reviewer only.

## Official Gemini CLI (optional, unverified here)

Not installed during development. If installed and separately authenticated (`gemini` login),
the adapter uses `gemini -p <prompt> --output-format json [--approval-mode auto_edit]`;
`detect()` checks for those flags first.

## Mindlogic (optional, disabled)

OpenAI-compatible HTTP API. Put `MINDLOGIC_API_KEY` in `/etc/agentbridge/agentbridge.env`
(mode 640, root:agentbridge) and set `base_url` / `model` in `agentbridge.yaml`, then enable it.
It is text-only (reviewer role), is never used as Tech Lead automatically, and its ~8,000
credits/month are **not** converted into tokens or money: budget it with invocation limits.
No endpoint or credentials were available during development, so it is unverified.

## GitHub

```bash
sudo -iu agentbridge gh auth login     # fine-grained PAT recommended
```

Minimal permissions for the selected repositories only: **Contents: read & write** (push
`agentbridge/*` branches), **Pull requests: read & write** (draft PRs), **Metadata: read**.
Add branch protection on `main` on GitHub as a second line of defence.
