#!/usr/bin/env bash
# AgentBridge installer for Ubuntu 22.04/24.04 (tested target: Intel N100 mini PC).
# Idempotent. Run from a checkout of the agentbridge repository:  sudo ./deployment/install.sh
# It does NOT log in to any AI provider and does NOT copy credentials.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/agentbridge DATA=/var/lib/agentbridge CONF=/etc/agentbridge REPOS=/srv/agentbridge/repos
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl sqlite3 rsync >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'

id agentbridge >/dev/null 2>&1 || useradd --system --create-home --home-dir "$DATA" --shell /bin/bash agentbridge
install -d -o agentbridge -g agentbridge -m 750 "$DATA" "$REPOS" /srv/agentbridge /var/backups/agentbridge
install -d -o root -g agentbridge -m 750 "$CONF"
install -d -o agentbridge -g agentbridge -m 700 "$DATA/identities"

rsync -a --delete --exclude .venv --exclude .git --exclude var "$SRC/" "$PREFIX/"
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q --upgrade pip
"$PREFIX/.venv/bin/pip" install -q "$PREFIX"

if [[ ! -f "$CONF/agentbridge.env" ]]; then
  tok=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  sec=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  printf 'AGENTBRIDGE_ADMIN_TOKEN=%s\nAGENTBRIDGE_SESSION_SECRET=%s\n' "$tok" "$sec" > "$CONF/agentbridge.env"
  chown root:agentbridge "$CONF/agentbridge.env"; chmod 640 "$CONF/agentbridge.env"
  echo "Generated admin token in $CONF/agentbridge.env (read it with: sudo cat $CONF/agentbridge.env)"
fi
[[ -f "$CONF/agentbridge.yaml" ]] || install -o root -g agentbridge -m 640 "$SRC/examples/agentbridge.example.yaml" "$CONF/agentbridge.yaml"

install -m 644 "$SRC"/deployment/systemd/*.service "$SRC"/deployment/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
sudo -u agentbridge env AGENTBRIDGE_CONFIG="$CONF/agentbridge.yaml" AGENTBRIDGE_DATA_DIR="$DATA" \
  "$PREFIX/.venv/bin/agentbridge" init
systemctl enable --now agentbridge-engine.service agentbridge-backup.timer
echo
echo "Engine installed. Control plane: either"
echo "  sudo systemctl enable --now agentbridge-api      (host)   or"
echo "  cd $PREFIX && docker compose up -d                (Docker; set AB_UID/AB_GID in .env)"
echo "Next: log in each agent identity as the agentbridge user (docs/AGENT_AUTH.md), then"
echo "  sudo -u agentbridge $PREFIX/.venv/bin/agentbridge agents detect"
