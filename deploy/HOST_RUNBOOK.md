# EVOLUTIONARY TRADING ALGO // VPS Host Provisioning Runbook

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

### 2.7 Optional — Cloudflare Tunnel for the dashboard

Skip if you only ever ssh in. If you want `https://apex.yourdomain.com`
to reach the Streamlit dashboard:

```bash
# See deploy/scripts/cloudflare_setup_named.ps1 for the Windows-side
# equivalent. On Linux:
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cf.deb
sudo dpkg -i cf.deb
cloudflared tunnel login
cloudflared tunnel create apex-live
# route: `apex.yourdomain.com -> http://localhost:8501`
sudo cloudflared service install <tunnel-token>
```

UFW stays closed to 8501 — the tunnel speaks loopback only.

---

## 3. Bot install

From here, follow `deploy/README.md`. Condensed:

```bash
# As apex user, NOT root:
sudo apt install -y git python3.14 python3.14-venv cron build-essential
git clone <your-fork> ~/eta_engine
cd ~/eta_engine && ./deploy/install_vps.sh
$EDITOR .env     # ANTHROPIC_API_KEY, active broker keys, PUSHOVER_*, TELEGRAM_*
```

### 3.1 .env minimum

```ini
APEX_MODE=PAPER                 # start PAPER; flip to LIVE only after smoke
ANTHROPIC_API_KEY=sk-ant-...
IBKR_ACCOUNT_ID=DU...
TASTY_ACCOUNT_NUMBER=...

# Mobile push (obs/mobile_push.py) -- see 4.4
PUSHOVER_APP_TOKEN=...
PUSHOVER_USER_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Permissions:

```bash
chmod 600 ~/eta_engine/.env
```

Tradovate remains **DORMANT** under the current broker mandate. Do not add
`TRADOVATE_*` credentials for normal readiness; only use the Appendix A
un-dormancy procedure if the operator explicitly reactivates that venue in
code and docs together.

### 3.2 First-boot smoke

```bash
cd ~/eta_engine
.venv/bin/python -m eta_engine.deploy.scripts.smoke_check
# should print: [PASS] jarvis, [PASS] venue_adapter, [PASS] kill_switch, ...
```

### 3.3 Enable services

```bash
systemctl --user enable --now jarvis-live avengers-fleet eta-dashboard
systemctl --user status jarvis-live
journalctl --user -u jarvis-live -f          # tail
```

### 3.4 Cron (chaos drills + drift detector + allowlist refresh)

```bash
crontab deploy/cron/avengers.crontab
crontab -l | grep eta-engine:avengers     # verify
```

---

## 4. Post-install safety gates

Do NOT skip. This is what keeps an Apex eval alive.

### 4.1 Chaos drill coverage

```bash
.venv/bin/python -m eta_engine.scripts._chaos_drill_matrix \
    --output reports/chaos_drill_matrix.md --fail-under 0
```

Read `reports/chaos_drill_matrix.md`. Any `[GAP]` row is an un-drilled
safety surface — fine for PAPER, not fine for LIVE.

### 4.2 Order-state reconcile

```bash
.venv/bin/python -c "
from eta_engine.core.order_state_reconcile import OrderStateReconciler
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
from eta_engine.obs.mobile_push import MobilePushBus, MobileAlert, MobileSeverity
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
sed -i 's/^APEX_MODE=.*/APEX_MODE=LIVE/' ~/eta_engine/.env
systemctl --user restart jarvis-live avengers-fleet eta-dashboard
journalctl --user -u jarvis-live -f | grep 'MODE='
```

First print should show `MODE=LIVE`.

---

## 5. Operator daily / weekly

| Cadence | Command | Reads |
|---|---|---|
| daily | `journalctl --user -u jarvis-live --since "24 hours ago" \| tail -200` | startup + breaker trips |
| daily | `.venv/bin/python -m eta_engine.scripts.deadman_check` | pulse log |
| weekly | `.venv/bin/python -m eta_engine.brain.jarvis_cost_attribution` | OPUS burn budget |
| weekly | `.venv/bin/python -m eta_engine.scripts._chaos_drill_matrix --fail-under 0` | regression in drill coverage |
| monthly | provider dashboard | snapshot restored / paid |

---

## 6. Disaster recovery

### 6.1 Provider blew up the box

Restore from snapshot. All state is in:
- `~/eta_engine/.env`           (secrets)
- `var/eta_engine/state/`       (journals, ledgers, model state)
- `logs/eta_engine/`            (audit trail)

Both `state` and `log` dirs are captured by provider-level snapshots.

### 6.2 You blew up the install

```bash
cd ~/eta_engine
./deploy/uninstall_vps.sh     # stops services, drops units
./deploy/install_vps.sh       # reinstalls fresh from source
# .env stays in place; state dir stays in place
```

### 6.3 LIVE account suspended / eval blown

1. `systemctl --user stop jarvis-live` immediately.
2. Flip `.env` back to `APEX_MODE=PAPER`.
3. Export the last 30 days of journals to `~/eta_engine/reports/postmortem/`.
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
