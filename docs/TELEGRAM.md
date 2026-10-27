# Telegram bot

Check quota and progress, and get notified, without touching the server.

- **Outbound only:** long polling to `api.telegram.org`, no webhook, no open port.
- **Buttons:** 📊 Quota · 📁 Dự án (projects) · 📝 Chờ duyệt (approvals) · 🔄 Làm mới (refresh).
  Commands: `/status`, `/quota`, `/approvals`.
- **Quota view:** for each enabled agent:
  - ✅ **real numbers reported by the provider:** Antigravity's `/usage` (Gemini, Claude/GPT and
    Argon groups), re-read for free when you open the view; and Claude Code's 5-hour and 7-day
    % left from its last call; Codex's 5-hour and weekly % from the session file Codex itself
    writes after each run. A window whose
    reset time has passed is shown as "đã reset (chưa đo lại)" (reset, not measured since), not
    as a stale number. `agentbridge agents smoke <id>` takes a fresh measurement for one tiny call.
  - Owner budgets (`rolling_limits`) still apply as a safety net but are not shown in the bot.
  - state: 🔴 cooldown (until the provider's reset time), 🟠 circuit open, ⏸ paused.
  Tap an agent for details.
- **Notifications** (configurable via `telegram.notify`): milestone completed, task completed,
  task blocked, approval requested, agent cooldown, agent lost its login, agent circuit open,
  quota fallback reroutes, engine stopped or restarted, internal engine errors, draft PR opened.
  The first start does not replay old history; a restart continues from a cursor stored in the DB.

## Setup

1. In Telegram, talk to **@BotFather** → `/newbot` and copy the token.
2. On the server, add the token to the env file (never to git or the yaml):
   ```bash
   sudoedit /etc/agentbridge/agentbridge.env        # TELEGRAM_BOT_TOKEN=123456:ABC...
   ```
3. Send `/start` to your new bot from your Telegram account, then find your chat id:
   ```bash
   agentbridge telegram --discover
   ```
4. Put that id in `/etc/agentbridge/agentbridge.yaml`:
   ```yaml
   telegram:
     allowed_chat_ids: [123456789]
     allow_approvals: false
     timezone: Asia/Ho_Chi_Minh
   ```
5. Test, then run it as a service:
   ```bash
   agentbridge telegram --test
   sudo systemctl enable --now agentbridge-telegram
   ```

## Security

- Updates from chats not in `allowed_chat_ids` are ignored (their id is only logged).
- `allow_approvals: true` adds Approve/Reject buttons. Anyone controlling your Telegram account
  could then approve requests, such as starting a milestone or opening a draft PR. Policy limits
  still apply (no protected-branch merges, hardware no-go), and each action is audited as
  `telegram:<user id>`. It is off by default.
- Messages contain redacted summaries only (task keys, states, short reasons). Logs, diffs,
  prompts and credentials are never sent. Note that these summaries do leave the server and go
  through Telegram.
- The token is read from `TELEGRAM_BOT_TOKEN`; HTTP client request logging is disabled so the
  token-bearing URL never reaches the journal.
