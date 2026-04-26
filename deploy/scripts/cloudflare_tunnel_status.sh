#!/usr/bin/env bash
# APEX PREDATOR // deploy/scripts/cloudflare_tunnel_status.sh
# ----------------------------------------------------------
# One-shot health check for the JARVIS Master Command Center
# tunnel. Run after install, after a reboot, or whenever the
# MCC stops being reachable from your phone.
#
# Reports (in order):
#   1. cloudflared service status (system-level systemd)
#   2. Tunnel registration + connector count (control-plane health)
#   3. Local upstream reachability (MCC on 127.0.0.1)
#   4. Cloudflare-side resolution + edge response
#
# Exit code is 0 if every gate passes, non-zero otherwise.
#
# Required: MCC_HOSTNAME (e.g. cmd.example.com) -- can be passed as
# arg 1 or env var. MCC_PORT defaults to 8765.
#
set -uo pipefail   # NB: not -e -- we want to keep going to surface every failure

: "${MCC_PORT:=8765}"
: "${MCC_TUNNEL_NAME:=jarvis-mcc}"
MCC_HOSTNAME="${1:-${MCC_HOSTNAME:-}}"

if [[ -z "${MCC_HOSTNAME}" ]]; then
    echo "usage: $0 <hostname>   # e.g. cmd.example.com" >&2
    echo "   or: MCC_HOSTNAME=cmd.example.com $0" >&2
    exit 2
fi

PASS=0
FAIL=0

ok()   { printf '  [ok]   %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  [BAD]  %s\n' "$*"; FAIL=$((FAIL+1)); }
sect() { printf '\n=== %s ===\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. cloudflared service
# ---------------------------------------------------------------------------
sect "1. cloudflared service"
if systemctl is-active --quiet cloudflared; then
    ok "service active"
else
    bad "service NOT active -- 'sudo systemctl status cloudflared' for details"
fi
if systemctl is-enabled --quiet cloudflared; then
    ok "service enabled (will survive reboot)"
else
    bad "service NOT enabled -- 'sudo systemctl enable cloudflared'"
fi

# ---------------------------------------------------------------------------
# 2. Tunnel registration + connector count
# ---------------------------------------------------------------------------
sect "2. tunnel registration"
if ! command -v cloudflared >/dev/null 2>&1; then
    bad "cloudflared binary missing -- run cloudflare_tunnel_setup.sh"
else
    if cloudflared tunnel list 2>/dev/null | awk 'NR>2 {print $2}' | grep -qx "${MCC_TUNNEL_NAME}"; then
        ok "tunnel '${MCC_TUNNEL_NAME}' is registered"
    else
        bad "tunnel '${MCC_TUNNEL_NAME}' not found in 'cloudflared tunnel list'"
    fi

    INFO=$(cloudflared tunnel info "${MCC_TUNNEL_NAME}" 2>/dev/null || true)
    if [[ -n "${INFO}" ]]; then
        # cloudflared tunnel info prints a CONNECTORS table after a header line.
        CONNECTORS=$(printf '%s\n' "${INFO}" | awk '/^ID/{found=1; next} found && NF>0 {n++} END {print n+0}')
        if [[ "${CONNECTORS}" -gt 0 ]]; then
            ok "control plane sees ${CONNECTORS} connector(s)"
        else
            bad "control plane sees zero connectors -- cloudflared not phoning home"
        fi
    else
        bad "could not fetch 'cloudflared tunnel info ${MCC_TUNNEL_NAME}'"
    fi
fi

# ---------------------------------------------------------------------------
# 3. Local upstream (MCC on 127.0.0.1:${MCC_PORT})
# ---------------------------------------------------------------------------
sect "3. local upstream (MCC)"
if curl -fsS --max-time 3 "http://127.0.0.1:${MCC_PORT}/healthz" >/dev/null; then
    ok "MCC /healthz on 127.0.0.1:${MCC_PORT} responds"
else
    bad "MCC NOT reachable on 127.0.0.1:${MCC_PORT} -- 'systemctl --user status jarvis-command-center'"
fi
if curl -fsS --max-time 3 "http://127.0.0.1:${MCC_PORT}/api/state" \
        | python3 -c "import sys, json; json.load(sys.stdin); print('  parsed', file=sys.stderr)" \
        2>/dev/null; then
    ok "MCC /api/state returns valid JSON"
else
    bad "MCC /api/state did not return valid JSON"
fi

# ---------------------------------------------------------------------------
# 4. Cloudflare edge
# ---------------------------------------------------------------------------
sect "4. Cloudflare edge"
if getent hosts "${MCC_HOSTNAME}" >/dev/null 2>&1; then
    ok "${MCC_HOSTNAME} resolves"
else
    bad "${MCC_HOSTNAME} does not resolve (DNS propagation or wrong hostname)"
fi

# Edge response: with Cloudflare Access in place we expect 302 -> CF Access
# login, 401, or 403 -- NOT 200 (200 means Access is missing -- security gap).
EDGE_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 8 "https://${MCC_HOSTNAME}/" || echo "000")
case "${EDGE_CODE}" in
    302|401|403)
        ok "edge returns ${EDGE_CODE} (Cloudflare Access gating active)"
        ;;
    200)
        bad "edge returns 200 -- Cloudflare Access is NOT configured; MCC is OPEN"
        ;;
    000)
        bad "edge unreachable (curl failed)"
        ;;
    *)
        bad "edge returns unexpected ${EDGE_CODE}"
        ;;
esac

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
sect "summary"
printf '  pass: %d   fail: %d\n' "${PASS}" "${FAIL}"
[[ "${FAIL}" -eq 0 ]] || exit 1
