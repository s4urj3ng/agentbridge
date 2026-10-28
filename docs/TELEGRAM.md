# Telegram bot

Check quota and progress, and get notified, without touching the server.

- **Outbound only:** long polling to `api.telegram.org`, no webhook, no open port.
- **Buttons:** 📊 Quota · 📁 Dự án (projects) · 📝 Chờ duyệt (approvals) · ⚙️ Phân công
  (assignment) · 🔄 Làm mới (refresh). Commands: `/status`, `/quota`, `/approvals`, `/assign`.
- **Quota view:** agents grouped by role (🧠 Tech Lead, 🛠 Code, 🔍 Review), with one aligned
  line per provider window, for example `5h ▓▓▓▓▓░░░░░ còn 46% ↻ 52p` (↻ = time until reset: minutes
  or hours within a day, otherwise weekday and time, e.g. `T7 14:13`). The header shows the current
  time in `telegram.timezone` (default `Asia/Seoul`, "giờ Hàn").
  - The numbers are the **real figures reported by the provider**:
    - Antigravity's `/usage` (Gemini, Claude/GPT and Argon groups), re-read for free when you open
      the view or tap 🔄 Làm mới;
    - Claude Code's 5-hour and 7-day usage from its last call;
    - Codex's 5-hour and weekly usage from the session file Codex writes after each run.
  - A window whose reset time has passed shows "đã reset · chưa đo lại" instead of a stale
    number, and measurements older than 3 hours are marked "đo N giờ trước".
    `agentbridge agents smoke <id>` takes a fresh measurement for one tiny call.
  - Owner budgets (`rolling_limits`) still apply as a safety net but are not shown in the bot.
  - State icons: 🟢 ready, 🟡 handing work to its fallbacks (provider stop level, with the reason and
    the fallback names), 🔴 cooldown or circuit open (with the end time), ⏸ paused.
  Tap an agent for details.
- **⚙️ Phân công (assignment):** one screen per project (opens directly when there is only one):
  ```
  ⚙️ Phân công · g3x-lite
  🛠 CODE (trên xuống = ưu tiên → dự phòng)
   1. antigravity-worker   [⬆️][⬇️][➖]
   2. antigravity-argon    [⬆️][⬇️][➖]
   [➕ Thêm vào Code]
  🔍 REVIEW
   1. antigravity-claude-reviewer  [⬆️][⬇️][➖]
   2. codex-reviewer               [⬆️][⬇️][➖]
   [➕ Thêm vào Review]
  [↩️ Hoàn tác] [♻️ Về gốc] [⬅️ Menu]
  ```
  - **One ordered list per role.** Item 1 of CODE is the default worker, item 1 of REVIEW the
    preferred reviewer. If an agent is refused for quota reasons, the work goes to the next
    admitted agent below it in the same list, then wraps round to the ones above. Reviews skip the
    task's author. The Tech Lead is shown read-only.
  - The icon after a name comes from the quota view: 🟡 handing work over (provider stop level),
    🔴 cooldown or circuit open, ⏸ paused or disabled.
  - With `telegram.allow_config_changes: true`, **each tap applies at once**: ⬆️ ⬇️ move, ➖
    removes (the last agent of a role cannot be removed), and ➕ lists enabled agents with that
    role that are not in the list yet (one tap adds to the end). **↩️ Hoàn tác** undoes the last
    change, one step per tap. **♻️ Về gốc** drops the project's override and goes back to the
    yaml/manifest. The message is edited to show the new order.
  - Tap an agent's name for a small screen with **⏸ Tắt / ▶️ Bật** and **🔢 Song song** (0–8),
    plus ↩️ / ♻️ for that agent.
  - Changes are saved in the database. They take precedence over the yaml and the manifest,
    survive restarts, apply to the next dispatch (running work is not stopped), and are audited as
    `telegram:<user id>`. Details: [ALLOCATION.md](ALLOCATION.md).
- **📝 Approvals:** every approval is a compact card: the action in Vietnamese (Bắt đầu milestone,
  Tạo draft PR, Gỡ chặn task, Cần quyết định), project · milestone · task, and the first line of the
  reason (≤160 chars).
  - The "Cần bạn duyệt" notification carries **✅ Duyệt / ❌ Từ chối / 📄 Xem đầy đủ** when
    `allow_approvals: true`, otherwise only 📄. The 📝 Chờ duyệt list uses the same cards.
  - **📄 Xem đầy đủ** sends the full reason plus evidence, split into several messages when it is long.
  - After a decision the notification is edited to "✅ Đã duyệt #id bởi bạn lúc HH:MM" (or ❌) and
    its buttons are removed. Tapping a resolved approval again only answers "Đã xử lý rồi".
- **Notifications** (configurable via `telegram.notify`): milestone completed, task completed,
  task blocked, approval requested, agent cooldown, agent lost its login, agent circuit open,
  quota fallback reroutes, engine stopped or restarted, internal engine errors, draft PR opened,
  assignment changes made outside Telegram and queued tasks moved by a new order (`config_change`).
  Taps on ⚙️ itself are not re-broadcast: the edited screen shows the result.
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
     timezone: Asia/Seoul             # default; times in the bot are Korea time
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
  works and reviews, in which order (and so the fallback order), enable/disable and concurrency.
  It is off by default; when off, the ⚙️ screen is read-only and every change button is refused.
  Each tap applies immediately, so the safety net is: owner chats only, an audit entry per change,
  ↩️ Hoàn tác, ♻️ Về gốc, and validation (known ids, enabled, correct role, never the last agent of
  a role, a task's author never reviews it). Hardware no-go, protected branches and the
  Antigravity external-model guard cannot be changed from Telegram.
- Messages contain redacted summaries only (task keys, states, short reasons). Logs, diffs,
  prompts and credentials are never sent. Note that these summaries do leave the server and go
  through Telegram.
- The token is read from `TELEGRAM_BOT_TOKEN`; HTTP client request logging is disabled so the
  token-bearing URL never reaches the journal.
