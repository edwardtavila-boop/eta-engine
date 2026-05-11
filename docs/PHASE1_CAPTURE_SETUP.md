# Phase 1 — IBKR Pro tick + depth capture setup

**Date:** 2026-05-08 (operator unlocked IBKR Pro Level 2 + CME realtime)
**Goal:** start capturing tick + depth history TODAY — every uncaptured day is irrecoverable.

This doc is the operator-facing runbook for getting Phase 1 of the
`IBKR_PRO_DATA_INVENTORY.md` upgrade path running on the VPS (where
TWS Gateway lives).

---

## Prerequisites

1. **TWS Gateway running** on the VPS at port 4002 (paper) or 4001 (live)
2. **IBKR Pro market-data subscriptions ACTIVE** for every exchange you trade — verify with the audit script (Step 0 below)
3. Python 3.11+ with `ib_insync` installed (`pip install ib_insync`)
4. Free disk space — expect ~50-200 MB/symbol/day for ticks, ~500 MB/symbol/day for depth (5-level @ 1Hz)

---

## Step 0 — Verify subscriptions are realtime (5 min, run ONCE manually first)

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.verify_ibkr_subscriptions
```

Expected output:
```
Exchange  Symbol  Type           Verdict
CME       MNQ     REALTIME       [OK] PASS
NYMEX     CL      REALTIME       [OK] PASS
COMEX     GC      REALTIME       [OK] PASS
CBOT      ZN      REALTIME       [OK] PASS
>>> ALL REALTIME — IBKR Pro subscriptions active across probed exchanges.
```

**If any exchange returns `DELAYED` or `FAIL`:**
1. Log into IBKR Account Management → Settings → User Settings → Market Data Subscriptions
2. Enable the missing exchange subscription (typically ~$1.50-15/month per exchange)
3. Wait ~5 minutes for activation
4. Re-run the verifier

**Do NOT start capture until the verifier passes** — capturing delayed data is worse than no data (creates false confidence).

---

## Step 1 — Start tick capture daemon (one-time setup)

The tick capture daemon subscribes to `reqTickByTickData` for the
pinned-bot symbol set and writes every trade tick to
`mnq_data/ticks/<SYMBOL>_<YYYYMMDD>.jsonl`.

### Test run (foreground, 30 seconds)

```powershell
cd C:\EvolutionaryTradingAlgo
python -m eta_engine.scripts.capture_tick_stream --port 4002
# Ctrl-C after 30 seconds; check that mnq_data\ticks\ has new files
```

### Production setup (Windows Task Scheduler, always-on)

Run as Administrator PowerShell:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Python314\python.exe" `
    -Argument "-m eta_engine.scripts.capture_tick_stream --port 4002" `
    -WorkingDirectory "C:\EvolutionaryTradingAlgo"

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # unlimited

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Highest

Register-ScheduledTask -TaskName "ETA-CaptureTicks" `
    -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "ETA tick-by-tick capture for IBKR Pro Phase 1"

# Start now (without rebooting)
Start-ScheduledTask -TaskName "ETA-CaptureTicks"
```

Verify it's running:
```powershell
Get-ScheduledTask -TaskName "ETA-CaptureTicks" | Get-ScheduledTaskInfo
```

---

## Step 2 — Start depth-snapshot daemon (same pattern)

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Python314\python.exe" `
    -Argument "-m eta_engine.scripts.capture_depth_snapshots --port 4002 --depth-rows 5 --snapshot-interval-ms 1000" `
    -WorkingDirectory "C:\EvolutionaryTradingAlgo"

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask -TaskName "ETA-CaptureDepth" `
    -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "ETA L2 depth snapshot capture for IBKR Pro Phase 1"

Start-ScheduledTask -TaskName "ETA-CaptureDepth"
```

**Note:** `--depth-rows 5` captures top 5 levels each side. Higher values
need the **CME Depth of Book** subscription enabled (~$15/mo separate
from base CME realtime).

---

## Step 3 — Verify capture is producing data

After 5 minutes of running:

```powershell
cd C:\EvolutionaryTradingAlgo
ls mnq_data\ticks\  | Where-Object { $_.Name -match (Get-Date -Format 'yyyyMMdd') }
ls mnq_data\depth\  | Where-Object { $_.Name -match (Get-Date -Format 'yyyyMMdd') }

# Expect 7-8 files in each dir (one per pinned-bot symbol)
# File sizes should be visibly growing
```

Run the health monitor:
```powershell
python -m eta_engine.scripts.capture_health_monitor
```

Expected output:
```
capture-health: GREEN  (0 issues)
  all symbols capturing freshly; subscription audit current
```

---

## Step 4 — Cloud-side daily monitoring (already configured)

Two cloud routines now check capture health daily without operator action:

1. **`eta: IBKR subscription audit (daily)`** — runs the verifier; alerts if any exchange goes delayed
2. **`eta: capture health monitor (daily)`** — checks tick + depth file freshness; alerts if a daemon crashed

Alerts land in `logs/eta_engine/alerts_log.jsonl` (also shipped to the dashboard).

---

## What happens after Phase 1 is running

- **Day 1+**: ticks accumulate in `mnq_data/ticks/` and depth snapshots in `mnq_data/depth/`
- **Day 7**: enough tick history to start Phase 2 (bar-builder with buy/sell volume split)
- **Day 14**: enough depth history to start Phase 3 (sweep_reclaim v2 + volume_profile v2 with real L2 confirmation)

---

## Storage planning

Expected disk usage (8 symbols × full RTH + ETH session):

| Type | Per symbol per day | All 8 symbols per day | 30 days |
|------|-------------------:|----------------------:|--------:|
| Ticks | 50-200 MB | 0.4-1.6 GB | 12-50 GB |
| Depth (5L @ 1Hz) | 100-500 MB | 0.8-4 GB | 25-120 GB |
| **Combined** | | **1.2-5.6 GB/day** | **37-170 GB/month** |

The VPS should have ≥500 GB free for ~3 months of capture. Rotate
older files to S3 / cold storage when needed; bar reconstruction
can read directly from compressed JSONL.

---

## Operator-eye TWS BookTrader (manual circuit breaker)

Per the upgrade plan: open a **TWS BookTrader window on MNQ** during
live trading so the operator can see L2 depth themselves before ETA
can read it. If the book looks broken (zero size on one side,
massive imbalance, ladder gaps), the operator can hit the kill switch
manually while ETA is still reading bar data.

This is a Phase-0 mitigation for the window between today and Phase 5
(L2-aware ETA dispatch).

---

## Rollback

If a capture daemon misbehaves:
```powershell
Stop-ScheduledTask -TaskName "ETA-CaptureTicks"
Stop-ScheduledTask -TaskName "ETA-CaptureDepth"
# (Re-enable later with Start-ScheduledTask)
```

Files already written are unaffected — captures are append-only JSONL.
