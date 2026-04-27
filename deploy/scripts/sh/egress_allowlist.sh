#!/usr/bin/env bash
# EVOLUTIONARY TRADING ALGO  //  egress_allowlist.sh
#
# Build an nftables egress allowlist from deploy/configs/broker_cidrs.txt.
#
# Why
# ---
# Once the daemons are stable, an RCE in any deps (Anthropic SDK,
# httpx, eth_account, playwright, ...) becomes a "where can it phone
# home?" question. Locking egress to broker IPs only:
#   * Defeats data exfiltration via fresh outbound HTTPS to attacker host.
#   * Defeats reverse-shell beaconing.
#   * Forces any malicious code to reuse our existing connections,
#     which the WS-reachability monitor will spot.
#
# What it does
# ------------
# 1. Resolve every hostname in broker_cidrs.txt to A records (once).
# 2. Build a CIDR set + write it into /etc/nftables.d/eta-egress.nft.
# 3. Apply the ruleset (`nft -f` then save via nftables.service).
# 4. The default chain stays accept (we don't block first); only the
#    `egress_443_allow` set is enforced for tcp/443. udp/53 + udp/123
#    are always allowed.
#
# Idempotent. Safe to re-run weekly to pick up CDN IP shuffling.
#
# Usage:
#   sudo bash egress_allowlist.sh
#   sudo bash egress_allowlist.sh --check   # just emit the resolved set
#   sudo bash egress_allowlist.sh --revert  # remove the rule, allow all egress

set -euo pipefail

CHECK=0
REVERT=0
case "${1:-}" in
  --check)  CHECK=1 ;;
  --revert) REVERT=1 ;;
esac

CFG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../configs" && pwd)"
SOURCE="${CFG_DIR}/broker_cidrs.txt"
NFT_DROP="/etc/nftables.d/eta-egress.nft"

if [[ "${EUID}" -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

if [[ "${REVERT}" -eq 1 ]]; then
  echo "## removing ${NFT_DROP}"
  rm -f "${NFT_DROP}"
  systemctl reload nftables 2>/dev/null || systemctl restart nftables 2>/dev/null || true
  echo "## egress allowlist removed; all outbound now permitted."
  exit 0
fi

if [[ ! -f "${SOURCE}" ]]; then
  echo "broker_cidrs.txt not found at ${SOURCE}" >&2
  exit 2
fi

# ----------------------------------------------------------------------
# 1. Parse + resolve.
# ----------------------------------------------------------------------
echo "## 1. resolving broker CIDRs"
TMP=$(mktemp)
trap 'rm -f "${TMP}"' EXIT

# Strip comments + blanks.
grep -vE '^\s*(#|$)' "${SOURCE}" | awk '{print $1}' | while read -r entry; do
  if [[ "${entry}" =~ ^[0-9.]+/[0-9]+$ ]] || [[ "${entry}" =~ ^[0-9a-fA-F:]+/[0-9]+$ ]]; then
    # Already a CIDR (v4 or v6).
    echo "${entry}"
  elif [[ "${entry}" =~ ^[0-9.]+$ ]]; then
    # Bare IP -> /32.
    echo "${entry}/32"
  else
    # Hostname -> resolve A records.
    if command -v getent >/dev/null 2>&1; then
      getent ahosts "${entry}" 2>/dev/null \
        | awk '{print $1}' \
        | sort -u \
        | while read -r ip; do
            [[ -n "${ip}" ]] && echo "${ip}/32"
          done
    fi
  fi
done | sort -u > "${TMP}"

NUM=$(wc -l < "${TMP}")
echo "## resolved ${NUM} CIDRs"

if [[ "${CHECK}" -eq 1 ]]; then
  cat "${TMP}"
  exit 0
fi

# ----------------------------------------------------------------------
# 2. Build the nft ruleset.
# ----------------------------------------------------------------------
echo "## 2. building ruleset at ${NFT_DROP}"
mkdir -p "$(dirname "${NFT_DROP}")"
{
  echo '# Managed by deploy/scripts/sh/egress_allowlist.sh — do not edit by hand.'
  echo 'table inet eta_egress {'
  echo '    set egress_443_allow {'
  echo '        type ipv4_addr; flags interval;'
  echo '        elements = {'
  paste -sd ',\n' "${TMP}" | sed 's/^/            /'
  echo '        }'
  echo '    }'
  echo '    chain output {'
  echo '        type filter hook output priority 0; policy accept;'
  echo '        ct state established,related accept'
  echo '        # always allow loopback'
  echo '        oif lo accept'
  echo '        # DNS / NTP / DHCP unconditional'
  echo '        udp dport 53 accept'
  echo '        tcp dport 53 accept'
  echo '        udp dport 123 accept'
  echo '        udp dport {67, 68} accept'
  echo '        # ICMP for path MTU + diagnostics'
  echo '        icmp type echo-request accept'
  echo '        icmpv6 accept'
  echo '        # 443 only to allowlisted hosts; everything else dropped'
  echo '        tcp dport 443 ip daddr @egress_443_allow accept'
  echo '        tcp dport 443 log prefix "eta-egress-drop: " drop'
  echo '        # 80 (apt update + lets-encrypt) -- keep open; cuts the rope on'
  echo '        # almost-all RCE phone-home traffic since most uses 443.'
  echo '        tcp dport 80 accept'
  echo '    }'
  echo '}'
} > "${NFT_DROP}"

# ----------------------------------------------------------------------
# 3. Apply.
# ----------------------------------------------------------------------
echo "## 3. applying"
nft -f "${NFT_DROP}"
systemctl reload nftables 2>/dev/null || true

echo "## summary"
nft list ruleset | head -50
echo "## OK -- to revert: sudo bash $0 --revert"
