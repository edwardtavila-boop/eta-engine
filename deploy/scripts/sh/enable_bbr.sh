#!/usr/bin/env bash
# EVOLUTIONARY TRADING ALGO  //  deploy/scripts/sh/enable_bbr.sh
#
# Enable TCP BBR congestion control + tune kernel network buffers for
# the long-poll websocket connections this fleet depends on (Bybit,
# OKX, Binance, IBKR, Tastytrade, TradingView).
#
# Why BBR vs CUBIC?  CUBIC reacts to loss; BBR models bandwidth-delay
# product. On a VPS with a noisy upstream (Hetzner / Linode / OVH), BBR
# typically halves p99 packet retransmits and keeps WS RTT flat under
# bursty cross-traffic.
#
# Why these buffer sizes?  Default rmem_max=212992 throttles WS reads
# on bursts (orderbook L2 snapshots can land 200+ frames at once).
# Bumping to 16 MiB matches cloud-provider best-practice ceilings.
#
# Idempotent: re-running just refreshes /etc/sysctl.d/99-bbr.conf and
# `sysctl --system`.
#
# Usage:
#   sudo bash enable_bbr.sh
#   sudo bash enable_bbr.sh --check     # report current settings, do nothing
#
# Exit codes:
#   0  applied (or already correct)
#   1  must run as root
#   2  kernel does not support BBR (need >= 4.9)

set -euo pipefail

CONF="/etc/sysctl.d/99-eta-bbr.conf"

print_current() {
  echo "current cc:        $(sysctl -n net.ipv4.tcp_congestion_control || echo unknown)"
  echo "available cc:      $(sysctl -n net.ipv4.tcp_available_congestion_control || echo unknown)"
  echo "default qdisc:     $(sysctl -n net.core.default_qdisc || echo unknown)"
  echo "rmem_max:          $(sysctl -n net.core.rmem_max || echo unknown)"
  echo "wmem_max:          $(sysctl -n net.core.wmem_max || echo unknown)"
  echo "tcp_rmem:          $(sysctl -n net.ipv4.tcp_rmem || echo unknown)"
  echo "tcp_wmem:          $(sysctl -n net.ipv4.tcp_wmem || echo unknown)"
  echo "somaxconn:         $(sysctl -n net.core.somaxconn || echo unknown)"
  echo "netdev_max_backlog:$(sysctl -n net.core.netdev_max_backlog || echo unknown)"
}

if [[ "${1:-}" == "--check" ]]; then
  print_current
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root (try: sudo bash $0)" >&2
  exit 1
fi

# Verify kernel supports BBR
if ! grep -qw bbr /proc/sys/net/ipv4/tcp_available_congestion_control 2>/dev/null; then
  # Try loading the module
  modprobe tcp_bbr 2>/dev/null || true
  if ! grep -qw bbr /proc/sys/net/ipv4/tcp_available_congestion_control 2>/dev/null; then
    echo "kernel lacks tcp_bbr support (needs >= 4.9)" >&2
    exit 2
  fi
fi

cat > "${CONF}" <<'EOF'
# Managed by deploy/scripts/sh/enable_bbr.sh -- do not edit by hand.
# BBR + buffer tuning for the Evolutionary Trading Algo VPS.

# Congestion control
net.core.default_qdisc            = fq
net.ipv4.tcp_congestion_control   = bbr

# Socket buffers (bytes). 16 MiB caps comfortably handle WS L2 bursts
# while leaving room for the GC+RSS profile of jarvis-live.
net.core.rmem_max                 = 16777216
net.core.wmem_max                 = 16777216
net.ipv4.tcp_rmem                 = 4096 87380 16777216
net.ipv4.tcp_wmem                 = 4096 65536 16777216

# Backlog + accept queue tuning -- the dashboard PWA + uvicorn benefit
# from larger queues during cold-start bursts.
net.core.somaxconn                = 4096
net.core.netdev_max_backlog       = 16384

# Faster TIME_WAIT recycle for the many short-lived HTTPS calls (alerts,
# Pushover, Telegram, Anthropic API). Safe on egress-heavy boxes.
net.ipv4.tcp_fin_timeout          = 15
net.ipv4.tcp_tw_reuse             = 1

# Keepalive: detect dead WS peers within ~3 minutes (default is 2 hours).
net.ipv4.tcp_keepalive_time       = 60
net.ipv4.tcp_keepalive_intvl      = 30
net.ipv4.tcp_keepalive_probes     = 4
EOF

sysctl --system >/dev/null
echo "BBR + buffers applied via ${CONF}"
print_current
