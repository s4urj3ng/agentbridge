# Web dashboard

`agentbridge-api` serves the dashboard on `127.0.0.1:8787`, next to the Telegram bot; both read
the same database and show the same numbers.

- **Tổng quan:** counters (running, waiting for review, queued, blocked, approvals, agent runs in
  24 h), one card per open milestone with its progress bar split by stage and every task, real
  provider quota grouped by role, and recent activity in plain words. The page refreshes itself
  every 20 seconds (paused while you type in a field).
- **Dự án / task:** dependency graph, per-milestone progress, task specification, validation,
  reviews, diff, attempts, logs. You can submit a goal, pause a project or cancel a task.
- **Agent & quota:** quota per role, then each identity's detection, login state and usage, with
  enable/disable, pause/resume and concurrency controls.
- **Chờ duyệt:** approve or reject with a note for the Tech Lead.

Times use `telegram.timezone` (default `Asia/Seoul`).

## Sign-in and hardening

Every page except `/login`, `/api/health` and `/webhooks/github` needs a 12-hour session cookie
from `/login`, or `Authorization: Bearer $AGENTBRIDGE_ADMIN_TOKEN` (scripts and the JSON API).
Form posts carry a CSRF token.

Sign in with a user name and password by adding them to `/etc/agentbridge/agentbridge.env`:

```bash
AGENTBRIDGE_WEB_USER=avp
AGENTBRIDGE_WEB_PASSWORD=<a long password>
```

then `sudo systemctl restart agentbridge-api`. With a password set the login page asks for user
name and password only (the token no longer signs in to the web, but still works for the API).
Without one it asks for the admin token. Changing the password or the token signs out every open
session.

For a public URL the app also:

- allows 5 failed logins per client IP per 15 minutes (`CF-Connecting-IP` behind Cloudflare), then
  answers 429; every failure is in the audit log ("Đăng nhập sai từ …" in the activity feed);
- marks the session cookie `Secure` and sends HSTS when the request came over HTTPS
  (`X-Forwarded-Proto: https`, set by cloudflared);
- sends `X-Frame-Options: DENY`, `nosniff`, a same-origin referrer policy and a CSP that only allows
  the app's own resources;
- asks search engines not to index the pages.

The login gives full control (approvals, cancelling tasks, agents). Use a long password, and
put Cloudflare Access in front of the hostname so that only your e-mail address reaches the login
page at all.

## Publishing through a Cloudflare Tunnel

No port is opened on the server: `cloudflared` connects out to Cloudflare. On the N100:

```bash
# 1. install cloudflared (Ubuntu)
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared

# 2. log in (opens a URL to approve in your browser) and create the tunnel
cloudflared tunnel login
cloudflared tunnel create agentbridge
cloudflared tunnel route dns agentbridge agentbridge.example.com
```

`~/.cloudflared/config.yml` (use the tunnel id that `tunnel create` printed):

```yaml
tunnel: <TUNNEL-ID>
credentials-file: /home/<user>/.cloudflared/<TUNNEL-ID>.json
ingress:
  - hostname: agentbridge.example.com
    service: http://127.0.0.1:8787
  - service: http_status:404
```

```bash
# 3. run it as a service
sudo cloudflared --config ~/.cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
```

Alternatively create the tunnel in the Cloudflare dashboard (Zero Trust → Networks → Tunnels),
add a public hostname pointing at `http://127.0.0.1:8787`, and run the `cloudflared service
install <token>` command it shows.

### Cloudflare Access (recommended)

Zero Trust → Access → Applications → Add → Self-hosted, hostname `agentbridge.example.com`,
policy "Allow" with your e-mail address (one-time PIN login). If GitHub webhooks are enabled, add a
second application for the path `/webhooks/github` with a "Bypass" policy: those requests are
authenticated by their HMAC signature instead.
