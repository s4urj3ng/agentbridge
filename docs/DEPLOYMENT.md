# Deployment on Ubuntu (Intel N100)

> Not yet performed on the target server. These steps match the files in `deployment/`, but
> they were written and unit-tested on macOS without access to the N100. Run the
> restart-recovery check below after installing.

## 1. Install

Ubuntu 24.04 ships Python 3.12. On 22.04, install python3.12 from deadsnakes first.

```bash
git clone <your agentbridge remote> ~/agentbridge && cd ~/agentbridge
sudo ./deployment/install.sh
```

The script creates the `agentbridge` system user (home `/var/lib/agentbridge`), installs to
`/opt/agentbridge/.venv`, creates `/etc/agentbridge/{agentbridge.env,agentbridge.yaml}`
(generated admin token, mode 640), installs and enables `agentbridge-engine.service` and the
daily backup timer, and runs `agentbridge init`. It does not log in to any provider.

It also installs `/usr/local/bin/agentbridge`, a wrapper that always runs the CLI as the
`agentbridge` user with `/etc/agentbridge/agentbridge.yaml` and `/var/lib/agentbridge`, so every
`agentbridge …` command below works from any sudo-capable account. Re-running `install.sh`
after `git pull` upgrades in place and keeps config, tokens and the database.

## 2. Provider CLIs and logins (as `agentbridge`)

Install the CLIs for the `agentbridge` user (for example `~/.local/bin/claude`, `agy`), then
follow [AGENT_AUTH.md](AGENT_AUTH.md). Check:

```bash
agentbridge agents detect
agentbridge agents smoke claude-team-lead
```

## 3. Control plane

**Option A, host (simplest on an N100):**

```bash
sudo systemctl enable --now agentbridge-api
```

**Option B, Docker Compose:**

```bash
cd /opt/agentbridge && sudo cp .env.example .env && sudoedit .env
#   AB_UID=$(id -u agentbridge) AB_GID=$(id -g agentbridge), plus the same tokens as agentbridge.env
sudo docker compose up -d --build
```

The container mounts `/var/lib/agentbridge` (rw) and `/etc/agentbridge` (ro), binds
`127.0.0.1:8787`, runs read-only with all capabilities dropped, and never runs agents.

## 4. Remote access via Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo tailscale serve --bg 8787        # https://<host>.<tailnet>.ts.net → 127.0.0.1:8787, tailnet only
```

Then sign in with the token from `/etc/agentbridge/agentbridge.env`. Restrict access in your
tailnet ACLs. Do **not** use `tailscale funnel` for the dashboard. If you need GitHub
webhooks, expose only the webhook path, or poll PR state instead.

## 5. Projects

```bash
sudo -u agentbridge git clone https://github.com/s4urj3ng/g3x-lite /srv/agentbridge/repos/g3x-lite
agentbridge project add /opt/agentbridge/examples/g3x-lite.yaml
```

The G3X Lite manifest sets `auto_start: false` and `STRICT_NO_GO`. A submitted goal creates an
approval request and nothing runs until you approve it. Run the synthetic demo first
(`agentbridge demo` with a scratch `--data-dir`), and only then submit PORT-9B.

## 6. Health checks

- `GET /api/health` → `{"db": true, "engine_running": true|false}` (engine liveness from its lock)
- `deployment/healthcheck.sh`: exit 0 only if the API answers and the engine is running
- Docker `HEALTHCHECK` on the API container; systemd `Restart=always` for both services
- `journalctl -u agentbridge-engine -f`

## 7. Restart recovery check (run after install)

```bash
# 1. start a long task (demo with a slow mock worker, or any real task)
# 2. while a task is RUNNING:
sudo systemctl kill -s KILL agentbridge-engine     # simulate a crash; systemd restarts it
journalctl -u agentbridge-engine -n 50              # look for the "recovery" audit entry
agentbridge events -n 20                            # attempt adopted or marked lost; task never COMPLETED early
# 3. full reboot test
sudo reboot   # after boot: running attempts are LOST (no wrapper), tasks requeued, loop continues
```

`KillMode=process` lets running agent subprocesses survive an engine restart so they can be
re-adopted. The automated equivalents are `tests/test_recovery.py`.

## 8. Backup and restore

The database is the only state that needs backing up; worktrees and artifacts can be recreated,
and branches live in the repositories. Provider credentials are never in the backup.

```bash
# daily via agentbridge-backup.timer → /var/backups/agentbridge (14 days kept); manual:
agentbridge backup /var/backups/agentbridge/manual.db

# restore
sudo systemctl stop agentbridge-engine agentbridge-api   # or: docker compose stop
agentbridge restore /var/backups/agentbridge/manual.db --force
sudo systemctl start agentbridge-engine agentbridge-api
```

`backup` uses SQLite's online backup API, so it is safe while running. `restore` refuses while
the engine holds its lock and keeps the replaced DB as `agentbridge.db.pre-restore`.

## 9. N100 sizing

Each agent CLI is a Node/Go process using roughly 200–600 MB RSS. Start with
`max_concurrent_agents: 2` on 8 GB RAM and raise it after watching memory. SQLite and the API
are negligible.
