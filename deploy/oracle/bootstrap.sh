#!/usr/bin/env bash
# One-time setup of a fresh Oracle Cloud Ampere A1 VM (Ubuntu 24.04 aarch64).
# Idempotent: safe to re-run. Usage (as the default `ubuntu` user):
#   curl -fsSL https://raw.githubusercontent.com/Agent299/CustomerSupportAgent/main/deploy/oracle/bootstrap.sh | bash
# or, after cloning: bash deploy/oracle/bootstrap.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Agent299/CustomerSupportAgent.git}"
APP_DIR="${APP_DIR:-/opt/support-agent}"
SWAP_SIZE="${SWAP_SIZE:-4G}"
DEPLOY_USER="${SUDO_USER:-$USER}"

log() { printf '\n==> %s\n' "$*"; }

log "Base packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git netfilter-persistent iptables-persistent

log "Docker Engine + Compose plugin (Docker's official apt repository)"
if ! command -v docker >/dev/null; then
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo usermod -aG docker "$DEPLOY_USER"
sudo systemctl enable --now docker

log "Kernel settings (OpenSearch requires vm.max_map_count >= 262144)"
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-opensearch.conf >/dev/null
sudo sysctl --system >/dev/null

log "Swap (${SWAP_SIZE}) as a safety net for memory spikes"
if ! sudo swapon --show | grep -q /swapfile; then
  sudo fallocate -l "$SWAP_SIZE" /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

log "Host firewall: Oracle's Ubuntu image rejects everything except SSH; allow HTTP/HTTPS"
for rule in "-p tcp --dport 80" "-p tcp --dport 443" "-p udp --dport 443"; do
  # shellcheck disable=SC2086
  sudo iptables -C INPUT -m state --state NEW $rule -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT -m state --state NEW $rule -j ACCEPT
done
sudo netfilter-persistent save >/dev/null

log "Application code in ${APP_DIR}"
if [ ! -d "$APP_DIR/.git" ]; then
  sudo mkdir -p "$APP_DIR"
  sudo chown "$DEPLOY_USER":"$DEPLOY_USER" "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi
mkdir -p "$APP_DIR/backups"

log "systemd timers (nightly schedule + gc, Postgres backup)"
sudo cp "$APP_DIR"/deploy/oracle/systemd/*.service "$APP_DIR"/deploy/oracle/systemd/*.timer /etc/systemd/system/
sudo sed -i "s/^User=.*/User=${DEPLOY_USER}/" /etc/systemd/system/support-agent-*.service
sudo systemctl daemon-reload
sudo systemctl enable --now support-agent-schedule.timer support-agent-gc.timer support-agent-backup.timer

ENV_FILE="$APP_DIR/deploy/oracle/.env"
if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/deploy/oracle/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  log "Created $ENV_FILE: fill it in, then run: $APP_DIR/deploy/oracle/deploy.sh"
else
  log "Done. Deploy with: $APP_DIR/deploy/oracle/deploy.sh"
fi
echo "(Log out and back in once so the docker group applies to ${DEPLOY_USER}.)"
