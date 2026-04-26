# Apex Predator // VPS Deployment

One-shot install + operator runbook for the JARVIS + Avengers stack.

---

## TL;DR — fresh VPS in 5 commands

```bash
# On the VPS, as the operator user (NOT root):
sudo apt update && sudo apt install -y git python3.12 python3.12-venv cron
git clone https://github.com/<you>/apex_predator.git ~/apex_predator
cd ~/apex_predator && ./deploy/install_vps.sh
$EDITOR .env                                   # fill in TRADOVATE_*, ANTHROPIC_API_KEY
sudo loginctl enable-linger $USER              # survive logout
```

Then:

```bash
systemctl --user start jarvis-live avengers-fleet apex-dashboard
systemctl --user enable jarvis-live avengers-fleet apex-dashboard
journalctl --user -u jarvis-live -f            # tail JARVIS
```

---

## What gets installed

| Component | Where | Runs |
|-----------|-------|------|
| Source code | `~/apex_predator/` | N/A |
| Virtualenv | `~/apex_predator/.venv/` | N/A |
| Env secrets | `~/apex_predator/.env` (chmod 600) | read-only |
| JARVIS live daemon | `systemd --user jarvis-live.service` | always |
| Avengers dispatcher | `systemd --user avengers-fleet.service` | always |
| **Master Command Center** | `systemd --user jarvis-command-center.service` (port 8765) | **always** |
| Dashboard backend (FastAPI API) | `systemd --user apex-dashboard.service` (port 8000) | always |
| Background tasks | crontab (12 entries tagged `apex-predator:avengers`) | per task cadence |
| State | `~/.local/state/apex_predator/` | writable by services |
| Logs | `~/.local/log/apex_predator/` | append-only |

---

## The Avengers cron schedule

From `deploy/cron/avengers.crontab`:

**ROBIN (Haiku tier — grunt work)**
| Task | Cadence |
|------|---------|
| `DASHBOARD_ASSEMBLE` | every minute |
| `LOG_COMPACT` | hourly :00 |
| `PROMPT_WARMUP` | 13:25 + 13:55 Mon-Fri (pre-market + pre-close) |
| `AUDIT_SUMMARIZE` | daily 06:00 |

**ALFRED (Sonnet tier — operational maintenance)**
| Task | Cadence |
|------|---------|
| `SHADOW_TICK` | every 5 minutes |
| `DRIFT_SUMMARY` | every 15 minutes |
| `KAIZEN_RETRO` | daily 23:00 |
| `DISTILL_TRAIN` | Sundays 02:00 |

**BATMAN (Opus tier — strategic heavy-brain)**
| Task | Cadence |
|------|---------|
| `TWIN_VERDICT` | daily 22:00 |
| `STRATEGY_MINE` | Mondays 03:00 |
| `CAUSAL_REVIEW` | 1st of month 04:00 |
| `DOCTRINE_REVIEW` | quarterly 05:00 |

All invocations go through `python -m deploy.scripts.run_task <TASK>`.

---

## Pre-flight check

Before starting services, run the smoke check:

```bash
cd ~/apex_predator && .venv/bin/python -m deploy.scripts.smoke_check
```

It verifies: imports, `.env` has required vars, state dirs writable, dispatch
works with a dry-run executor, all 12 task handlers are wired, systemd units
are installed, and crontab has the Avengers entries.

Use `--skip-systemd` if running BEFORE `install_vps.sh` completes.

---

## Required `.env` variables

The install script appends a Claude layer stanza to `.env`. Fill these at minimum:

```
ANTHROPIC_API_KEY=sk-ant-...
JARVIS_HOURLY_USD_BUDGET=1.00
JARVIS_DAILY_USD_BUDGET=10.00
JARVIS_DISTILL_SKIP_THRESHOLD=0.92
```

Plus the standard apex_predator secrets (see `.env.example`):
- `TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD`, `TRADOVATE_CID`, `TRADOVATE_APP_SECRET`
- Any broker-specific entries (tastytrade, IBKR, etc.)

---

## Services

### `jarvis-live.service`
The JARVIS context engine + supervisor loop. Hot-path risk-gate.
- `WorkingDirectory`: repo root
- `ExecStart`: `python -m apex_predator.scripts.jarvis_live --interval 60`
- Writes: `~/.local/state/apex_predator/jarvis_live_health.json`
- Restart: `always`
- Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`

### `avengers-fleet.service`
The Avengers dispatcher daemon. Holds the Fleet + CostGovernor in memory.
- Depends on `jarvis-live.service`
- `ExecStart`: `python -m deploy.scripts.avengers_daemon`
- Writes: `avengers_heartbeat.json`, `usage_tracker.json`, `distiller.json`
- Restart: `always`

### `jarvis-command-center.service`
The **Master Command Center** — JARVIS operator console (PWA).
- Depends on `jarvis-live.service`
- `ExecStart`: `python -m apex_predator.scripts.jarvis_dashboard`
- Listens: `127.0.0.1:8765`
- Routes: `/`, `/api/state`, `/healthz`, `/manifest.webmanifest`, `/sw.js`, `/icon.svg`
- Remote access: Cloudflare Tunnel (loopback-only; no public ports)

### `apex-dashboard.service`
The FastAPI backend for the React trading dashboard.
- Depends on `jarvis-live.service`
- Listens: `127.0.0.1:8000`
- Reverse-proxy with Caddy/nginx if exposing externally

### `cloudflared` (system-level, optional)
Cloudflare Named Tunnel that exposes the Master Command Center at
`https://cmd.<your-domain>`, gated by Cloudflare Access. Set up via
`./deploy/scripts/cloudflare_tunnel_setup.sh`; verify with
`./deploy/scripts/cloudflare_tunnel_status.sh`. See
`deploy/HOST_RUNBOOK.md` §2.7.

---

## Common operations

```bash
# Status
systemctl --user status jarvis-live
systemctl --user status avengers-fleet

# Restart after .env change
systemctl --user restart jarvis-live avengers-fleet

# Live logs
journalctl --user -u jarvis-live -f
journalctl --user -u avengers-fleet -f
tail -f ~/.local/log/apex_predator/cron.log          # cron task output

# Manual task fire (useful when debugging)
cd ~/apex_predator && .venv/bin/python -m deploy.scripts.run_task KAIZEN_RETRO

# Quota / cost check
cat ~/.local/state/apex_predator/avengers_heartbeat.json | jq
```

---

## Upgrade flow

```bash
cd ~/apex_predator
git pull
./deploy/install_vps.sh       # idempotent -- re-runs everything safely
systemctl --user restart jarvis-live avengers-fleet apex-dashboard
```

---

## Uninstall / rollback

```bash
./deploy/uninstall_vps.sh               # stop services, strip cron
./deploy/uninstall_vps.sh --purge       # also rm state + logs
# Source code + .env preserved -- delete manually if desired.
```

---

## Security posture

- Services run as the operator user, **never root**.
- `.env` is `chmod 600`; no other user can read it.
- systemd hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
  `ProtectHome=read-only`, `ReadWritePaths` whitelist, `MemoryDenyWriteExecute`.
- Hardening details in `obs/vps_hardening.py` (UFW + SSHD + fail2ban configs).
- ANTHROPIC_API_KEY only read by the avengers-fleet service (never by bots).

---

## Troubleshooting

**`systemctl --user` says 'Unit not found'**
→ Run `systemctl --user daemon-reload`. If that fails, check that
`~/.config/systemd/user/` has the `.service` files.

**Services die after logout**
→ You need `loginctl enable-linger $USER`. This is a one-time sudo op.

**Cron fires but nothing happens**
→ Check `~/.local/log/apex_predator/cron.log`. Likely `PATH` issue; cron starts
with a minimal env. The cronfile sets `PATH` explicitly — if you edited it,
ensure `/usr/local/bin` + `/usr/bin` are still in the list.

**ANTHROPIC_API_KEY set but no Claude calls**
→ Check `avengers_heartbeat.json`. If `quota_state == FREEZE`, burn ceiling hit.
Bump `JARVIS_HOURLY_USD_BUDGET` in `.env` and restart the service.

**Dashboard not loading**
→ `curl http://127.0.0.1:8000/health`. If that works but browser doesn't,
you probably need a reverse proxy (Caddy/nginx) if accessing externally.

---

## File index

```
deploy/
├── README.md                       # this file
├── HOST_RUNBOOK.md                 # VPS provisioning runbook
├── install_vps.sh                  # idempotent installer
├── uninstall_vps.sh                # safe rollback
├── systemd/
│   ├── jarvis-live.service
│   ├── avengers-fleet.service
│   ├── jarvis-command-center.service   # Master Command Center (port 8765)
│   └── apex-dashboard.service
├── cron/
│   └── avengers.crontab            # 12 scheduled tasks, tagged
├── config/
│   └── (reserved for future YAML configs)
└── scripts/
    ├── run_task.py                       # single-task cron entry point
    ├── avengers_daemon.py                # systemd long-running daemon
    ├── smoke_check.py                    # pre-flight verification
    ├── cloudflare_tunnel_setup.sh        # MCC tunnel installer (Linux)
    └── cloudflare_tunnel_status.sh       # MCC tunnel health check
```
