#!/usr/bin/env bash
# EVOLUTIONARY TRADING ALGO  //  tune_disk.sh
#
# Disk + memory tuning for the journal-heavy workload.
#
# What this does
# --------------
#
# 1. **fstab noatime,nodiratime** on the root filesystem -- removes
#    metadata writes on every read, halves disk-write IOPS during a
#    journal scan storm.
#
# 2. **fstrim weekly timer** -- enables fstrim.timer (already shipped
#    with util-linux on Debian 12+) so NVMe wear-leveling stays sharp.
#    Without trim, a year-old VPS sees ~3-4x p99 write latency.
#
# 3. **zram-swap** -- creates a compressed-RAM swap device sized at
#    25% of total memory. When a flushed gzip + Chromium GC + Anthropic
#    SDK token-window all land at once, zram absorbs the pressure
#    without blocking journal appends on disk swap-out.
#
# 4. **vm.swappiness = 10** -- even with zram, prefer page-cache
#    eviction over swap. We have far more file-IO than allocations.
#
# Idempotent.
#
# Usage:
#   sudo bash tune_disk.sh
#   sudo bash tune_disk.sh --dry-run

set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

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

# ----------------------------------------------------------------------
# 1. fstab noatime
# ----------------------------------------------------------------------
echo "## 1. fstab noatime,nodiratime on /"
if grep -E '^\S+\s+/\s+(ext4|xfs|btrfs)' /etc/fstab | grep -qv noatime; then
  echo "  patching /etc/fstab"
  run "cp /etc/fstab /etc/fstab.bak.\$(date +%s)"
  # Add noatime,nodiratime to the / line if missing.
  run "sed -i -E '/^\\S+\\s+\\/\\s+(ext4|xfs|btrfs)/{/noatime/!s/(\\s+(ext4|xfs|btrfs)\\s+)([^[:space:]]+)/\\1\\3,noatime,nodiratime/}' /etc/fstab"
  echo "  remount with new options:"
  run "mount -o remount /"
else
  echo "  / already noatime; skipping"
fi

# ----------------------------------------------------------------------
# 2. fstrim weekly
# ----------------------------------------------------------------------
echo "## 2. fstrim.timer"
run "systemctl enable --now fstrim.timer"
run "systemctl status fstrim.timer --no-pager --lines=0 || true"

# ----------------------------------------------------------------------
# 3. zram swap
# ----------------------------------------------------------------------
echo "## 3. zram swap"
if ! command -v zramctl >/dev/null 2>&1; then
  run "apt-get install -yq zram-tools"
fi

# zram-tools writes to /etc/default/zramswap on Debian/Ubuntu.
ZRAM_CONF=/etc/default/zramswap
if [[ -f "${ZRAM_CONF}" ]]; then
  run "sed -i 's/^#\\?PERCENT=.*/PERCENT=25/' ${ZRAM_CONF}"
  run "sed -i 's/^#\\?ALGO=.*/ALGO=zstd/' ${ZRAM_CONF}"
fi
run "systemctl enable --now zramswap"
run "systemctl restart zramswap"

# ----------------------------------------------------------------------
# 4. swappiness
# ----------------------------------------------------------------------
echo "## 4. vm.swappiness=10"
SYSCTL=/etc/sysctl.d/99-eta-vm.conf
cat <<'EOF' > "${SYSCTL}.tmp"
# Managed by deploy/scripts/sh/tune_disk.sh
vm.swappiness = 10
vm.vfs_cache_pressure = 50
# Background flush kicks in earlier so a flushed-gzip stall is bounded.
vm.dirty_background_ratio = 5
vm.dirty_ratio            = 15
EOF
if [[ "${DRY_RUN}" -eq 0 ]]; then
  mv "${SYSCTL}.tmp" "${SYSCTL}"
  sysctl --system >/dev/null
else
  rm -f "${SYSCTL}.tmp"
fi
echo "  applied"

echo "## summary"
run "free -h"
run "swapon --show"
run "findmnt / -o OPTIONS"
