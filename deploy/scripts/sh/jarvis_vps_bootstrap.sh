#!/usr/bin/env bash
# EVOLUTIONARY TRADING ALGO  //  deploy/scripts/sh/jarvis_vps_bootstrap.sh
#
# One-shot bootstrap for a fresh Linux VPS that will host the jarvis-live +
# avengers-fleet + tradingview-capture stack.
#
# What this does:
#   1. Refresh apt + install OS deps (chromium needs lots of system libs)
#   2. Install + configure: chrony, fail2ban, ufw, unattended-upgrades, redis
#   3. Drop the eta-engine configs (chrony, fail2ban jail, unattended-upgrades)
#   4. Run enable_bbr.sh + ufw_baseline.sh
#   5. Install playwright Chromium
#   6. Install prometheus-node-exporter for off-VPS metrics scraping
#
# It does NOT:
#   * Install the eta-engine python deps (operator runs `make install` after)
#   * Generate the auth-state JSON for TradingView (operator runs locally)
#   * Apply the systemd units (operator runs `register_fleet_tasks.ps1` /
#     `systemctl --user daemon-reload`)
#
# Usage:
#   sudo bash jarvis_vps_bootstrap.sh
#   sudo bash jarvis_vps_bootstrap.sh --dry-run     # print what would run
#   sudo bash jarvis_vps_bootstrap.sh --skip-bbr    # skip kernel net tuning
#
# Exit codes:
#   0  bootstrap complete
#   1  must run as root
#   2  apt-get failed
#   3  required tool missing (curl, etc.)

set -euo pipefail

DRY_RUN=0
SKIP_BBR=0
SKIP_UFW=0
SKIP_PLAYWRIGHT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)        DRY_RUN=1; shift ;;
    --skip-bbr)       SKIP_BBR=1; shift ;;
    --skip-ufw)       SKIP_UFW=1; shift ;;
    --skip-playwright) SKIP_PLAYWRIGHT=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "DRY-RUN: $*"
  else
    echo "+ $*"
    eval "$@"
  fi
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
CONFIG_DIR="${REPO_ROOT}/deploy/configs"

echo "========================================"
echo "Evolutionary Trading Algo VPS bootstrap"
echo "  REPO_ROOT=${REPO_ROOT}"
echo "  CONFIG_DIR=${CONFIG_DIR}"
echo "========================================"

# ----------------------------------------------------------------------
# 1. APT refresh + base packages
# ----------------------------------------------------------------------
echo "## 1. apt update + install"
export DEBIAN_FRONTEND=noninteractive
run "apt-get update -qq"
run "apt-get install -yq --no-install-recommends \
  ca-certificates curl gnupg jq \
  chrony \
  fail2ban \
  ufw \
  unattended-upgrades apt-listchanges \
  redis-server \
  prometheus-node-exporter \
  python3-venv python3-pip \
  rsync \
  htop iftop iotop"

# ----------------------------------------------------------------------
# 2. Chromium runtime libs (for playwright headless captures)
# ----------------------------------------------------------------------
if [[ "${SKIP_PLAYWRIGHT}" -eq 0 ]]; then
  echo "## 2. chromium runtime libraries"
  # The list comes from `playwright install-deps chromium --dry-run` on
  # Debian 12 / Ubuntu 22.04. Pinning explicitly avoids surprise pull-ins
  # on the next playwright minor.
  run "apt-get install -yq --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libgbm1 libxkbcommon0 libpango-1.0-0 libcairo2 libasound2 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libxshmfence1 \
    fonts-liberation"
fi

# ----------------------------------------------------------------------
# 3. Drop configs
# ----------------------------------------------------------------------
echo "## 3. install configs"
run "install -m 644 ${CONFIG_DIR}/chrony.conf /etc/chrony/chrony.conf"
run "install -m 644 ${CONFIG_DIR}/sshd-jail.local /etc/fail2ban/jail.local"
run "install -m 644 ${CONFIG_DIR}/50unattended-upgrades.local \
                    /etc/apt/apt.conf.d/50unattended-upgrades.local"

# ----------------------------------------------------------------------
# 4. Kernel net tuning (BBR + buffers)
# ----------------------------------------------------------------------
if [[ "${SKIP_BBR}" -eq 0 ]]; then
  echo "## 4. enable BBR + tune buffers"
  run "bash ${SCRIPT_DIR}/enable_bbr.sh"
fi

# ----------------------------------------------------------------------
# 5. Firewall
# ----------------------------------------------------------------------
if [[ "${SKIP_UFW}" -eq 0 ]]; then
  echo "## 5. ufw deny-by-default baseline"
  run "bash ${SCRIPT_DIR}/ufw_baseline.sh"
fi

# ----------------------------------------------------------------------
# 6. Restart services to pick up new configs
# ----------------------------------------------------------------------
echo "## 6. restart services"
run "systemctl enable --now chrony"
run "systemctl restart chrony"
run "systemctl enable --now fail2ban"
run "systemctl restart fail2ban"
run "systemctl enable --now unattended-upgrades"
run "systemctl enable --now redis-server"
run "systemctl restart redis-server"
run "systemctl enable --now prometheus-node-exporter"

# ----------------------------------------------------------------------
# 7. Sanity output
# ----------------------------------------------------------------------
echo "## 7. status"
run "systemctl --no-pager --lines=0 status chrony fail2ban ufw redis-server prometheus-node-exporter || true"
run "chronyc tracking | head -10 || true"
run "ufw status | head -10 || true"

echo "========================================"
echo "bootstrap complete."
echo "  Next steps (run as your eta-engine user, NOT root):"
echo "    1. clone the repo into ~/eta-engine"
echo "    2. python3 -m venv .venv && source .venv/bin/activate"
echo "    3. pip install -e .[dev]"
if [[ "${SKIP_PLAYWRIGHT}" -eq 0 ]]; then
  echo "    4. .venv/bin/playwright install chromium"
fi
echo "    5. cp .env.example .env && fill in secrets"
echo "    6. systemctl --user daemon-reload && systemctl --user enable --now jarvis-live avengers-fleet"
echo "========================================"
