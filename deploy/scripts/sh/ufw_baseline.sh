#!/usr/bin/env bash
# EVOLUTIONARY TRADING ALGO  //  deploy/scripts/sh/ufw_baseline.sh
#
# Provision a deny-by-default ufw firewall for the VPS:
#
#   ingress   ssh (22), nothing else by default
#   egress    443 (api/ws), 53 (dns), 123 (ntp); deny rest
#
# All operator-facing surfaces (MCC PWA, FastAPI dashboard) are reached
# via Cloudflare Tunnel -- the tunnel daemon is the only inbound path.
# So we do NOT open 8000/8443 here.
#
# Idempotent.
#
# Usage:
#   sudo bash ufw_baseline.sh
#   sudo bash ufw_baseline.sh --status     # report rules, do nothing
#   sudo bash ufw_baseline.sh --ssh-port 2222
#
# Exit codes:
#   0  applied (or already configured)
#   1  must run as root
#   2  ufw not installed
#   3  bad argument

set -euo pipefail

SSH_PORT=22

while [[ $# -gt 0 ]]; do
  case "$1" in
    --status)    ufw status verbose; exit 0 ;;
    --ssh-port)  SSH_PORT="$2"; shift 2 ;;
    *)           echo "unknown arg: $1" >&2; exit 3 ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

if ! command -v ufw >/dev/null 2>&1; then
  echo "ufw not installed; run: apt-get install -y ufw" >&2
  exit 2
fi

# Reset to a known baseline (idempotent: ufw reset is safe; --force suppresses prompt).
ufw --force reset >/dev/null

# ---- ingress ----
ufw default deny incoming
ufw allow "${SSH_PORT}"/tcp comment 'sshd'

# ---- egress ----
# Default deny is too aggressive for most VPS workflows (apt, pip, etc.)
# but CRITICAL for a fund-control box. Allow common outbound.
ufw default allow outgoing
# Optional belt-and-suspenders egress lockdown:
#   ufw default deny outgoing
#   ufw allow out 443/tcp comment 'https'
#   ufw allow out 80/tcp  comment 'http (apt, lets-encrypt)'
#   ufw allow out 53      comment 'dns'
#   ufw allow out 123/udp comment 'ntp'

ufw logging low

ufw --force enable >/dev/null
ufw status verbose
