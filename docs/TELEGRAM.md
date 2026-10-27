# Telegram bot

Check quota and progress, and get notified, without touching the server.

- **Outbound only:** long polling to `api.telegram.org`, no webhook, no open port.
- **Buttons:** 📊 Quota · 📁 Dự án (projects) · 📝 Chờ duyệt (approvals) · ⚙️ Phân công
  (assignment) · 🔄 Làm mới (refresh). Commands: `/status`, `/quota`, `/approvals`, `/assign`.
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
- **⚙️ Phân công (assignment):** shows each project's Tech Lead, worker order and reviewer order,
  and each agent's fallback chain, enabled state and concurrency (✏️ = overridden at runtime).
  With `telegram.allow_config_changes: true` you can change them:
  - project → **👷 Worker mặc định** (pick one), **📋 Thứ tự worker** / **🔍 Thứ tự reviewer**
    (tap agents in order of preference, then ✅ Xong; 🧹 Chọn lại starts over);
  - agent → **↪️ Thứ tự dự phòng** (fallback order), **🔢 Song song** (0–8), **⏸ Tắt / ▶️ Bật**;
  - **♻️ Về cấu hình gốc** drops the overrides of that project/agent.
  Every change goes through a **✅ Xác nhận** step and is then validated, saved in the database
  (it takes precedence over the yaml and manifest and survives restarts), audited as
  `telegram:<user id>`, and announced to all allowed chats. It applies to the next dispatch;
  running work is not stopped. Details: [ALLOCATION.md](ALLOCATION.md).
- **Notifications** (configurable via `telegram.notify`): milestone completed, task completed,
  task blocked, approval requested, agent cooldown, agent lost its login, agent circuit open,
  quota fallback reroutes, engine stopped or restarted, internal engine errors, draft PR opened,
  assignment changes (`config_change`).
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
     allow_config_changes: false      # true = change assignment from ⚙️ Phân công
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
- `allow_config_changes: true` lets the allowed chats change standing configuration: which agent
  works and reviews, in which order, fallbacks, enable/disable and concurrency. It is off by
  default; when off the ⚙️ view is read-only and every change button is refused. Choices are
  validated (known ids, enabled, correct role, a task's author never reviews it); hardware no-go,
  protected branches and the Antigravity external-model guard cannot be changed from Telegram.
- Messages contain redacted summaries only (task keys, states, short reasons). Logs, diffs,
  prompts and credentials are never sent. Note that these summaries do leave the server and go
  through Telegram.
- The token is read from `TELEGRAM_BOT_TOKEN`; HTTP client request logging is disabled so the
  token-bearing URL never reaches the journal.
