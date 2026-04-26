#!/usr/bin/env bash
# APEX PREDATOR // deploy/scripts/cloudflare_tunnel_setup.sh
# ----------------------------------------------------------
# Idempotent installer for the Cloudflare Named Tunnel that
# fronts the JARVIS Master Command Center.
#
# Result: https://${MCC_HOSTNAME} (e.g. cmd.example.com) reaches
#   localhost:${MCC_PORT} on this VPS, with Cloudflare Access
#   gating the entry. UFW stays closed -- the tunnel speaks
#   loopback only.
#
# Re-run safely: every step checks for existing state.
#
# Required (set as env vars or fill the prompts):
#   MCC_DOMAIN        e.g. example.com   -- a zone you own in Cloudflare
#   MCC_HOSTNAME      e.g. cmd.example.com   (defaults to cmd.${MCC_DOMAIN})
#   MCC_PORT          MCC bind port (default 8765)
#   MCC_TUNNEL_NAME   tunnel name in Cloudflare (default jarvis-mcc)
#
# After this script runs, finish in the Cloudflare dashboard:
#   Zero Trust -> Access -> Applications -> Add -> Self-hosted
#     Application domain: ${MCC_HOSTNAME}
#     Policy: Allow your operator email(s)
#
set -euo pipefail

log()  { printf '[mcc-tunnel] %s\n' "$*"; }
warn() { printf '[mcc-tunnel] WARN: %s\n' "$*" >&2; }
die()  { printf '[mcc-tunnel] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Inputs
# ---------------------------------------------------------------------------
: "${MCC_PORT:=8765}"
: "${MCC_TUNNEL_NAME:=jarvis-mcc}"

if [[ -z "${MCC_DOMAIN:-}" ]]; then
    read -r -p "Cloudflare zone you own (e.g. example.com): " MCC_DOMAIN
fi
[[ -n "${MCC_DOMAIN}" ]] || die "MCC_DOMAIN is required"

: "${MCC_HOSTNAME:=cmd.${MCC_DOMAIN}}"

log "host     = ${MCC_HOSTNAME}"
log "tunnel   = ${MCC_TUNNEL_NAME}"
log "upstream = http://localhost:${MCC_PORT}"

# ---------------------------------------------------------------------------
# 1. Install cloudflared from Cloudflare's apt repo (auto-updates, signed)
# ---------------------------------------------------------------------------
if command -v cloudflared >/dev/null 2>&1; then
    log "cloudflared already installed: $(cloudflared --version | head -1)"
else
    log "installing cloudflared from cloudflare apt repo..."
    sudo mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
        | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y cloudflared
    log "installed: $(cloudflared --version | head -1)"
fi

# ---------------------------------------------------------------------------
# 2. Authenticate (interactive on first run)
# ---------------------------------------------------------------------------
CF_DIR="${HOME}/.cloudflared"
mkdir -p "${CF_DIR}"

if [[ -f "${CF_DIR}/cert.pem" ]]; then
    log "cloudflared already authenticated (cert.pem present)"
else
    log "first-run auth -- opening browser to Cloudflare; pick the ${MCC_DOMAIN} zone"
    cloudflared tunnel login
    [[ -f "${CF_DIR}/cert.pem" ]] || die "auth did not produce cert.pem"
fi

# ---------------------------------------------------------------------------
# 3. Create tunnel if not exists
# ---------------------------------------------------------------------------
if cloudflared tunnel list 2>/dev/null | awk 'NR>2 {print $2}' | grep -qx "${MCC_TUNNEL_NAME}"; then
    TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | awk -v n="${MCC_TUNNEL_NAME}" '$2==n {print $1}')
    log "tunnel '${MCC_TUNNEL_NAME}' already exists (${TUNNEL_ID})"
else
    log "creating tunnel '${MCC_TUNNEL_NAME}'..."
    cloudflared tunnel create "${MCC_TUNNEL_NAME}"
    TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | awk -v n="${MCC_TUNNEL_NAME}" '$2==n {print $1}')
    [[ -n "${TUNNEL_ID}" ]] || die "tunnel created but id not found"
    log "created: ${TUNNEL_ID}"
fi

CREDS_FILE="${CF_DIR}/${TUNNEL_ID}.json"
[[ -f "${CREDS_FILE}" ]] || die "tunnel creds file missing: ${CREDS_FILE}"

# ---------------------------------------------------------------------------
# 4. DNS route -- idempotent (re-run is a no-op if route already exists)
# ---------------------------------------------------------------------------
log "ensuring DNS: ${MCC_HOSTNAME} -> ${MCC_TUNNEL_NAME}..."
if cloudflared tunnel route dns "${MCC_TUNNEL_NAME}" "${MCC_HOSTNAME}" 2>&1 | tee /tmp/mcc_dns.log; then
    log "DNS routed"
else
    if grep -qiE "(already exists|record .* is already pointing)" /tmp/mcc_dns.log; then
        log "DNS route already present"
    else
        warn "cloudflared tunnel route dns returned non-zero; check /tmp/mcc_dns.log"
    fi
fi
rm -f /tmp/mcc_dns.log

# ---------------------------------------------------------------------------
# 5. Write config.yml -- ingress points at MCC loopback
# ---------------------------------------------------------------------------
CONFIG="${CF_DIR}/config.yml"
NEW_CONFIG=$(mktemp)
cat > "${NEW_CONFIG}" <<EOF
# Generated by deploy/scripts/cloudflare_tunnel_setup.sh
tunnel: ${MCC_TUNNEL_NAME}
credentials-file: ${CREDS_FILE}

# Sane defaults; tweak only if you know what you're doing.
no-autoupdate: false
metrics: 127.0.0.1:8766

ingress:
  - hostname: ${MCC_HOSTNAME}
    service: http://localhost:${MCC_PORT}
    originRequest:
      noTLSVerify: false
      connectTimeout: 10s
      tlsTimeout: 10s
      keepAliveConnections: 4
      keepAliveTimeout: 90s
  - service: http_status:404
EOF

if [[ -f "${CONFIG}" ]] && cmp -s "${CONFIG}" "${NEW_CONFIG}"; then
    log "config.yml already up to date"
    rm -f "${NEW_CONFIG}"
else
    mv "${NEW_CONFIG}" "${CONFIG}"
    log "wrote ${CONFIG}"
fi

# ---------------------------------------------------------------------------
# 6. Validate config before installing the service
# ---------------------------------------------------------------------------
log "validating config..."
cloudflared tunnel --config "${CONFIG}" ingress validate
cloudflared tunnel --config "${CONFIG}" ingress rule "https://${MCC_HOSTNAME}/healthz" \
    || warn "ingress rule check did not match expected hostname"

# ---------------------------------------------------------------------------
# 7. Install + start service
# ---------------------------------------------------------------------------
# cloudflared service install reads ~/.cloudflared/config.yml when present and
# wires up a system-level systemd unit. Idempotent: if already installed it
# only refreshes the config.
if systemctl is-enabled cloudflared >/dev/null 2>&1; then
    log "cloudflared service already installed; restarting to pick up config..."
    sudo systemctl restart cloudflared
else
    log "installing cloudflared as a system service..."
    sudo cloudflared --config "${CONFIG}" service install
    sudo systemctl enable --now cloudflared
fi

sleep 2
sudo systemctl status cloudflared --no-pager --lines=10 || true

# ---------------------------------------------------------------------------
# 8. Final summary + Cloudflare Access reminder
# ---------------------------------------------------------------------------
cat <<EOF

================================================================
  JARVIS MCC tunnel is up.
================================================================

  hostname       https://${MCC_HOSTNAME}
  tunnel         ${MCC_TUNNEL_NAME} (${TUNNEL_ID})
  upstream       http://localhost:${MCC_PORT}
  service        sudo systemctl status cloudflared

LAST STEP -- Cloudflare Access (gate the URL behind your email):

  1. https://one.dash.cloudflare.com -> Zero Trust -> Access -> Applications
  2. Add -> Self-hosted
  3. Application domain: ${MCC_HOSTNAME}
  4. Identity providers: enable Google or One-time PIN
  5. Add policy: Allow -> Emails ending in @<your-email-domain>
                       (or specific operator emails)
  6. Save.

Until you complete that step, ${MCC_HOSTNAME} will return 403 -- which
is the safe default. The MCC binds 127.0.0.1 only and is unreachable
without going through this tunnel + Access.

After Access is set up, install the PWA on your phone:
  1. Open https://${MCC_HOSTNAME} in Safari/Chrome on your phone.
  2. Sign in via Cloudflare Access.
  3. Browser menu -> Add to Home Screen / Install app.
  4. Done. Tap the icon to command JARVIS from anywhere.

EOF
