# APEX PREDATOR // VPS Host Provisioning Runbook

> Operator runbook for standing up the JARVIS + Avengers stack on a fresh
> cloud host. Companion to `deploy/README.md` (which assumes the box
> exists and you have SSH). This doc is what you read BEFORE that one.

---

## 0. Why we run on a VPS at all

Local Windows boxes die. Laptop lids close. Power blips kill positions.
The kill switch, order reconciler, and mobile push channel only keep
the operator safe if they run 24/7 on a machine that does not go to
sleep. That is the VPS's job.

| Surface | Local dev | VPS |
|---|---|---|
| Research, backtests | [YES] fast | [no] high-latency |
| Paper trading | [YES] sandbox | [YES] shadow |
| **LIVE execution** | **never** | **only place** |
| Mobile push + breakers | [BEST-EFFORT] | [ALWAYS-ON] |

The VPS is the only host permitted to flip `APEX_MODE=LIVE`.

---

## 1. Host pick — what to rent

The bot is single-process Python 3.14 + a Streamlit dashboard + a handful
of cron-scheduled daemons. It is **CPU + network** bound, not GPU, and
peak steady-state is a few hundred MB resident.

### Recommended baseline (2026-04)

| Tier | CPU | RAM | Disk | Region | Notes |
|---|---:|---:|---:|---|---|
| **Minimum** | 2 vCPU | 2 GB | 40 GB SSD | us-east-1 / Chicago / NY | Runs; tight headroom on Parquet rebuilds. |
| **Recommended** | 2 vCPU | 4 GB | 80 GB SSD | us-east-1 / Chicago / NY | Comfortable. Pick this. |
| **Overkill** | 4 vCPU | 8 GB | 160 GB SSD | us-east-1 / Chicago / NY | Only if running the full 14-stage sweep in-session. |

Region rule: **pick the region closest to your active venue gateway**
— IBKR Client Portal + Tastytrade API are both US East (2026-04-24: IBKR primary,
Tastytrade fallback; Tradovate DORMANT — funding-blocked). Every extra ms of
RTT shows up as slippage.

### Provider shortlist

| Provider | Why / why not |
|---|---|
| **Hetzner Cloud** (CX22, ~4.5 EUR/mo) | Best $/perf. EU-only is a latency tax for US futures — fine for research, not for LIVE. |
| **Vultr HF** (NJ or Chicago, ~$12/mo) | Fast CPUs, good US-East region, solid uptime. **Default pick for LIVE.** |
| **DigitalOcean** (NYC3, ~$12/mo) | Fine. Slightly slower CPUs than Vultr HF. |
| **AWS Lightsail / t4g.small** | Cheap but noisy-neighbor. Avoid for LIVE. |
| **Linode** (Newark) | Solid alternative to Vultr. |

**Not recommended for LIVE:** free tiers, $5/mo shared tiers, anything
with burstable CPU (the bot is steady-state; burst credits expire and
slippage spikes).

### OS

- **Ubuntu 24.04 LTS (Noble Numbat)** — CI baseline, all systemd units
  target this. Do not deviate.

---

## 2. Provisioning checklist

Work top-down. Every step is idempotent unless noted.

### 2.1 On the provider dashboard

- [ ] Spin up the VM with the chosen tier + Ubuntu 24.04.
- [ ] Attach an SSH key (your existing `~/.ssh/id_ed25519.pub`). **Do
      not use password auth.**
- [ ] Enable automatic backups / snapshots at the provider level. Daily
      is fine.
- [ ] Tag the box `apex-live` (or `apex-paper` if this is the shadow
      node) — makes billing reports legible later.

### 2.2 First login as root

```bash
ssh root@<ip>
apt update && apt upgrade -y
apt install -y ufw unattended-upgrades fail2ban
```

### 2.3 Create the operator user (never run as root)

```bash
adduser --disabled-password --gecos "" apex
usermod -aG sudo apex
rsync --archive --chown=apex:apex ~/.ssh /home/apex
echo 'apex ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/apex    # optional, only if you trust yourself
```

### 2.4 Lock SSH

Edit `/etc/ssh/sshd_config.d/99-apex.conf`:

```
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AllowUsers apex
```

Then:

```bash
systemctl reload ssh
# verify from a SECOND terminal:
ssh apex@<ip>
```

### 2.5 Firewall

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp        # SSH
# Only open the dashboard port if you plan to reach it directly.
# Prefer Cloudflare Tunnel (see 2.7) -- then leave this OFF.
# ufw allow 8501/tcp
ufw enable
```

### 2.6 Lingering / time

```bash
sudo loginctl enable-linger apex     # systemd user services survive logout
sudo timedatectl set-timezone UTC    # all logs + schedules run on UTC
sudo timedatectl set-ntp true
```

### 2.7 Cloudflare Tunnel for the Master Command Center

The **Master Command Center** (`scripts/jarvis_dashboard.py`) is the canonical
operator console. It's exposed remotely via a Cloudflare Named Tunnel so the
VPS keeps no public ports open and you reach it from any phone or laptop at
`https://cmd.<your-domain>`. Auth is handled at the edge by **Cloudflare
Access** (no app-side credentials).

```bash
# One command -- idempotent, safe to re-run.
MCC_DOMAIN=<your-domain> ./deploy/scripts/cloudflare_tunnel_setup.sh
```

The script will:

1. Install `cloudflared` from Cloudflare's signed apt repo (auto-updates).
2. Run `cloudflared tunnel login` interactively the first time (browser auth,
   pick your zone). Skipped on re-runs.
3. Create the `jarvis-mcc` tunnel if it doesn't exist.
4. Route DNS for `cmd.<your-domain>` to the tunnel.
5. Write `~/.cloudflared/config.yml` with ingress pointed at
   `http://localhost:8765` (the MCC).
6. Validate the config, install the system-level `cloudflared` service,
   enable + start it.
7. Print the next-step checklist for Cloudflare Access.

**Last manual step — Cloudflare Access** (gate the URL behind your email).
The script prints this at the end; for reference:

> **one.dash.cloudflare.com → Zero Trust → Access → Applications → Add → Self-hosted**
> - Application domain: `cmd.<your-domain>`
> - Identity providers: enable Google or One-time PIN
> - Policy: **Allow → Emails ending in `@<your-email-domain>`** (or specific operator emails)
> - Save

Until that's done, `cmd.<your-domain>` returns `403`, which is the safe default.
The MCC binds `127.0.0.1` only — the only path in is via this tunnel + Access.

**Verify any time:**

```bash
MCC_HOSTNAME=cmd.<your-domain> ./deploy/scripts/cloudflare_tunnel_status.sh
```

Reports cloudflared service state, tunnel registration, connector count, local
upstream reachability (MCC on `127.0.0.1:8765`), and edge response. Flags 200
at the edge as a **failure** (means Access isn't configured — security gap).

**Install on your phone:** open `https://cmd.<your-domain>` in Safari/Chrome,
sign in via Cloudflare Access, then browser menu → **Add to Home Screen** /
**Install app**. The MCC's manifest, theme, and offline shell are served by
the MCC itself (`/manifest.webmanifest`, `/sw.js`, `/icon.svg`).

UFW stays closed to 8765 — the tunnel speaks loopback only.

Windows / mac equivalents: `deploy/scripts/cloudflare_setup_named.ps1`.

---

## 3. Bot install

From here, follow `deploy/README.md`. Condensed:

```bash
# As apex user, NOT root:
sudo apt install -y git python3.14 python3.14-venv cron build-essential
git clone <your-fork> ~/apex_predator
cd ~/apex_predator && ./deploy/install_vps.sh
$EDITOR .env     # TRADOVATE_*, ANTHROPIC_API_KEY, PUSHOVER_*, TELEGRAM_*
```

### 3.1 .env minimum

```ini
APEX_MODE=PAPER                 # start PAPER; flip to LIVE only after smoke
TRADOVATE_USERNAME=...
TRADOVATE_PASSWORD=...
TRADOVATE_APP_ID=...
TRADOVATE_APP_VERSION=1.0
TRADOVATE_CID=...
TRADOVATE_SECRET=...
ANTHROPIC_API_KEY=sk-ant-...

# Mobile push (obs/mobile_push.py) -- see 4.4
PUSHOVER_APP_TOKEN=...
PUSHOVER_USER_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Permissions:

```bash
chmod 600 ~/apex_predator/.env
```

### 3.2 First-boot smoke

```bash
cd ~/apex_predator
.venv/bin/python -m apex_predator.deploy.scripts.smoke_check
# should print: [PASS] jarvis, [PASS] venue_adapter, [PASS] kill_switch, ...
```

### 3.3 Enable services

```bash
systemctl --user enable --now jarvis-live avengers-fleet apex-dashboard
systemctl --user status jarvis-live
journalctl --user -u jarvis-live -f          # tail
```

### 3.4 Cron (chaos drills + drift detector + allowlist refresh)

```bash
crontab deploy/cron/avengers.crontab
crontab -l | grep apex-predator:avengers     # verify
```

---

## 4. Post-install safety gates

Do NOT skip. This is what keeps an Apex eval alive.

### 4.1 Chaos drill coverage

```bash
.venv/bin/python -m apex_predator.scripts._chaos_drill_matrix \
    --output reports/chaos_drill_matrix.md --fail-under 0
```

Read `reports/chaos_drill_matrix.md`. Any `[GAP]` row is an un-drilled
safety surface — fine for PAPER, not fine for LIVE.

### 4.2 Order-state reconcile

```bash
.venv/bin/python -c "
from apex_predator.core.order_state_reconcile import OrderStateReconciler
print(OrderStateReconciler(conservative=True).reconcile({}, {}))
"
```

Should print a report with no divergence. Conservative mode is the
only acceptable mode on LIVE.

### 4.3 Kill switch dry-run

```bash
systemctl --user stop jarvis-live                   # simulate crash
# Should trigger avengers-fleet deadman -> breaker OPEN -> push alert
systemctl --user start jarvis-live
```

Confirm a push notification arrives on your phone. If not, fix 4.4
before going LIVE.

### 4.4 Mobile push round-trip

```bash
.venv/bin/python -c "
from apex_predator.obs.mobile_push import MobilePushBus, MobileAlert, MobileSeverity
bus = MobilePushBus.from_env()
print(bus.publish(MobileAlert(
    severity=MobileSeverity.CRITICAL,
    title='APEX install smoke',
    body='If you see this on your phone the push channel works.',
    source='install_runbook',
)))
"
```

Expect `{'suppressed': False, 'results': {'pushover': True, 'telegram': True}, ...}`
and a phone buzz within a few seconds.

### 4.5 LIVE flip

ONLY after 4.1 – 4.4 all pass:

```bash
sed -i 's/^APEX_MODE=.*/APEX_MODE=LIVE/' ~/apex_predator/.env
systemctl --user restart jarvis-live avengers-fleet apex-dashboard
journalctl --user -u jarvis-live -f | grep 'MODE='
```

First print should show `MODE=LIVE`.

---

## 5. Operator daily / weekly

| Cadence | Command | Reads |
|---|---|---|
| daily | `journalctl --user -u jarvis-live --since "24 hours ago" \| tail -200` | startup + breaker trips |
| daily | `.venv/bin/python -m apex_predator.scripts.deadman_check` | pulse log |
| daily | `MCC_HOSTNAME=cmd.<your-domain> ./deploy/scripts/cloudflare_tunnel_status.sh` | MCC tunnel + Access health |
| weekly | `.venv/bin/python -m apex_predator.brain.jarvis_cost_attribution` | OPUS burn budget |
| weekly | `.venv/bin/python -m apex_predator.scripts._chaos_drill_matrix --fail-under 0` | regression in drill coverage |
| monthly | provider dashboard | snapshot restored / paid |

---

## 6. Disaster recovery

### 6.1 Provider blew up the box

Restore from snapshot. All state is in:
- `~/apex_predator/.env`           (secrets)
- `~/.local/state/apex_predator/`   (journals, ledgers, model state)
- `~/.local/log/apex_predator/`     (audit trail)

Both `state` and `log` dirs are captured by provider-level snapshots.

### 6.2 You blew up the install

```bash
cd ~/apex_predator
./deploy/uninstall_vps.sh     # stops services, drops units
./deploy/install_vps.sh       # reinstalls fresh from source
# .env stays in place; state dir stays in place
```

### 6.3 LIVE account suspended / eval blown

1. `systemctl --user stop jarvis-live` immediately.
2. Flip `.env` back to `APEX_MODE=PAPER`.
3. Export the last 30 days of journals to `~/apex_predator/reports/postmortem/`.
4. Open a new Apex eval. Do NOT restart LIVE until operator sign-off.

---

## 7. Version pin

| Component | Pinned version | Bumped by |
|---|---|---|
| Ubuntu | 24.04 LTS | manual, annual |
| Python | 3.14 | `install_vps.sh` |
| Dependencies | `uv.lock` | CI |
| Tradovate API | V1 | external |

---

_Generated by the Firm. Pairs with `deploy/README.md`._
