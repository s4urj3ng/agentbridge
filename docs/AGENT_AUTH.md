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

agy keeps its login, settings (including tool permissions) and state in `$HOME/.gemini`. With an
empty `HOME`, `agy models` answers "Please sign in" (verified 2026-10-30). The default identities
use the `agentbridge` user's own home. The prompt is passed as an argument, so it is capped below
Linux's 128 KiB per-argument limit.

```bash
sudo -iu agentbridge
agy            # complete the login flow once
agy models     # should list models
```

### Several Antigravity accounts

Give each extra account its own home with `home_dir` in the identity's config; AgentBridge exports
it as `HOME` for that identity only. Tokens are never copied between accounts. Copy only the tool
permission file (`settings.json`), which holds no credentials.

```bash
sudo -iu agentbridge
N=2                                              # then 3 for the third account
H=/var/lib/agentbridge/identities/antigravity-$N
mkdir -p "$H/.gemini/antigravity-cli" && chmod 700 "$H"
cp ~/.gemini/antigravity-cli/settings.json "$H/.gemini/antigravity-cli/"   # tool permissions only
HOME=$H ~/.local/bin/agy                         # sign in with THIS account, then /exit
HOME=$H ~/.local/bin/agy models | head -3        # must list models
```

Then add identities with `home_dir: /var/lib/agentbridge/identities/antigravity-N` (templates
are in `examples/agentbridge.example.yaml`), restart the services and check them:
`agentbridge agents detect <id>` should show `isolated_identity: verified` and that account's own
`/usage` numbers. Each account's quota is probed separately.

Before adding accounts, confirm that Google's terms for your plan allow running several accounts
in an automated pool. AgentBridge does not bypass limits: each account's own limits and cooldowns
still apply.

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

## Mindlogic (아주LLM) API Gateway (optional, disabled by default)

Gateway facts used by the adapter (docs.mindlogic.ai → API Gateway, checked 2026-10-27):

- OpenAI-compatible base URL `https://factchat-cloud.mindlogic.ai/v1/gateway`; paths end in `/`
  (`/chat/completions/`, `/models/`, `/credits/`).
- Anthropic-native base URL `https://factchat-cloud.mindlogic.ai/v1/gateway/claude`. With a custom
  base URL, Claude Code needs `ANTHROPIC_AUTH_TOKEN`, not `ANTHROPIC_API_KEY`.
- One key reaches many providers' models; `GET /models/` lists what your organisation may use.
- `GET /credits/` returns the real balance. Rate limit: 120 requests / 60 s (429, sometimes
  without Retry-After).

Setup:

```bash
sudoedit /etc/agentbridge/agentbridge.env        # add: MINDLOGIC_API_KEY=<key>   (never commit it)
sudoedit /etc/agentbridge/agentbridge.yaml       # add identities from examples/agentbridge.example.yaml
sudo systemctl restart agentbridge-engine agentbridge-api
agentbridge agents detect                        # checks key (/credits/), model availability (/models/), balance
agentbridge agents enable mindlogic-gpt-reviewer
agentbridge agents smoke mindlogic-gpt-reviewer
```

Identities you can define with the same key:

| Identity type | Provider | Role | Edits files | How it is billed |
|---|---|---|---|---|
| `mindlogic-*-reviewer` (any LLM id from `/models/`) | `mindlogic` | reviewer | no | gateway credits |
| `claude-mindlogic-worker` | `claude` with `api_base_url` + `auth_token_env` | worker | yes (Claude Code tools) | gateway credits |

Budgeting: credits are not converted into tokens or money. Use `credit_floor` (calls are refused
below the real remaining balance and the identity cools down) together with
`daily_invocation_limit`. Mindlogic identities are never used as Tech Lead automatically.

The gateway adapter is tested against a local fake gateway (`tests/test_mindlogic.py`). Real
gateway calls still need verifying with `agents smoke` on the server.

## GitHub

```bash
sudo apt-get install -y gh
sudo -iu agentbridge gh auth login     # fine-grained PAT recommended; paste it, choose HTTPS
sudo -iu agentbridge gh auth setup-git # git push over HTTPS uses the same token
```

Minimal permissions for the selected repositories only: **Contents: read & write** (push
`agentbridge/*` branches), **Pull requests: read & write** (draft PRs), **Metadata: read**.
Add branch protection on `main` on GitHub as a second line of defence.

Then in `/etc/agentbridge/agentbridge.yaml`:

```yaml
github:
  enabled: true
  step_prs: true     # also a draft PR per task, not only one per milestone
```

With `step_prs`, just before a task is merged into the milestone's integration branch AgentBridge
pushes the integration branch and the task branch and opens a draft PR
`agentbridge/<project>/<milestone>/<task>` → `agentbridge/<project>/<milestone>/integration`
with the task's description, acceptance criteria, worker summary and every review. Its diff is
exactly that step. After the merge the integration branch is pushed again and GitHub shows the
step PR as merged into the integration branch; `main` is never touched (the milestone PR into
`main` stays a draft for you). A GitHub failure is audited (`github.step_pr_failed`) and never
holds up integration. Note that on a public repository every pushed branch is public.
