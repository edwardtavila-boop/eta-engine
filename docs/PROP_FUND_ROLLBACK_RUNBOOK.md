# Prop Fund Rollback Runbook

**Purpose:** what to do when the live prop-fund launch goes sideways.

**Last updated:** 2026-05-12 (wave-24, pre-launch)

> **Safety note:** Treat `python -m eta_engine.scripts.prop_launch_check --json`
> as the current Diamond/Wave-25 launch authority. If it is `NO_GO` or `HOLD`,
> keep all bots in `EVAL_PAPER` after recovery. The separate futures
> prop-ladder controlled dry-run lane remains a parallel story and does not
> override the launch gate.

---

## When to use this runbook

Any of:

1. The drawdown guard fires HALT and you don't know why.
2. The supervisor places an order you didn't expect.
3. The prop-fund account shows a P&L excursion outside expected bounds.
4. The IBKR connection is dropping repeatedly.
5. The dashboard shows red flags you can't interpret.
6. The prop firm sends an account-warning notice.

---

## Severity ladder — pick the right level of response

| Level | Symptoms | Response |
|-------|----------|----------|
| **L1 OBSERVE** | Single WATCH event, no DD breach, position sizing looks reasonable | Watch one more cycle (15 min). Don't intervene. |
| **L2 PAUSE** | HALT fires, but no positions open or all small. Or single WATCH lasting >2 cycles | Disable the prop-fund cron tasks (see §A); leave existing positions alone. |
| **L3 FLATTEN** | HALT + open positions. Or any unexpected order. Or daily DD > 60% of limit. | Disable cron, flatten ALL prop positions immediately (see §B). |
| **L4 LOCKDOWN** | Daily/static DD breached, account warning, or supervisor showing erratic behavior | L3 + revoke IBKR API access for the prop account (see §C). Page operator. |

**Default to higher severity if uncertain.** Re-enabling is cheaper than recovering from a voided account.

---

## §A — Pause cron tasks (L2)

Stops new entries from happening but leaves existing positions alone.

```powershell
# RDP / SSH to the VPS
ssh forex-vps

# Stop the 3 cron tasks that drive prop-fund routing
schtasks /End /TN "ETA-Diamond-LedgerEvery15Min"
schtasks /End /TN "ETA-Diamond-PropDrawdownGuardEvery15Min"
schtasks /End /TN "ETA-Diamond-LeaderboardHourly"

# Optional but recommended: stop the supervisor task too if it
# exists as a named scheduled task (varies by deployment)
# schtasks /End /TN "ETA-Supervisor"

# Verify they're stopped
schtasks /Query /TN "ETA-Diamond-LedgerEvery15Min" /FO LIST | findstr Status
```

These are the minimum entry-driving tasks. Observability-only ETA-Diamond tasks
such as the ops dashboard, watchdog, and alert dispatcher can stay up if you want
continued telemetry during the rollback.

After pausing, the existing positions ride out per their stops/targets. The flag files (`prop_halt_active.flag`, `prop_watch_active.flag`) remain in their last-written state until you re-enable the cron.

---

## §B — Flatten ALL prop positions (L3)

You MUST do this manually through the IBKR interface — the supervisor's flatten path requires the cron to be alive, which contradicts L2. This is intentional: the broker UI is the most reliable interface in a crisis.

### Step 1: identify open prop positions
```powershell
# On VPS, list current positions known to the supervisor
type C:\EvolutionaryTradingAlgo\var\eta_engine\state\open_positions\*.json
```

### Step 2: flatten via IBKR TWS or web interface
- Log into the prop-fund IBKR account
- Open the **Positions** panel
- For each PROP_READY position (m2k, met, mes_v2 by default — see latest leaderboard for the actual list), submit a **market order** opposite the position to close it
- Verify the position quantity goes to zero

### Step 3: emit a halt flag manually so the supervisor refuses re-entry
```powershell
# Write a halt flag the supervisor will see on its next tick
$payload = @{
    ts = (Get-Date -Format "o")
    rationale = "operator_manual_flatten_L3"
    prop_ready_bots = @()
} | ConvertTo-Json

$flag = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\prop_halt_active.flag"
$payload | Out-File -FilePath $flag -Encoding utf8
```

This file will be cleared the next time the drawdown guard runs and signal=OK; until then, the supervisor refuses prop-fund entries.

### Step 4: confirm via dashboard
- Check the dashboard alerts panel for any new RED entries
- Run `python -m eta_engine.scripts.diamond_prop_drawdown_guard` to confirm signal state
- Check `diamond_prop_dispatcher_latest.json` to see if push alerts were sent

---

## §C — IBKR API lockdown (L4)

Revoke the supervisor's ability to place orders. This is the strongest intervention — undo only after the issue is diagnosed.

### Option 1: TWS-side lockdown (fastest)
- In TWS: **Edit > Global Configuration > API > Settings**
- Uncheck "Enable ActiveX and Socket Clients"
- Apply

This kills the supervisor's broker connection immediately. Supervisor will log connection failures every tick but place no orders.

### Option 2: Account-level (safest, slower)
- In IBKR Account Management: revoke API access for the prop account
- Contact prop-firm support if the account is on a third-party rules service

After lockdown, follow §B Step 2 to manually flatten any remaining positions.

---

## §D — How to re-enable after a clean recovery

Only proceed when:
- The root cause of the incident is understood
- Daily DD has reset (next trading day)
- `python -m eta_engine.scripts.prop_launch_check --json` returns `GO`

```powershell
# 1. Clear any stale halt flag
Remove-Item C:\EvolutionaryTradingAlgo\var\eta_engine\state\prop_halt_active.flag -ErrorAction SilentlyContinue
Remove-Item C:\EvolutionaryTradingAlgo\var\eta_engine\state\prop_watch_active.flag -ErrorAction SilentlyContinue

# 2. Re-enable the cron tasks
schtasks /Run /TN "ETA-Diamond-LedgerEvery15Min"
schtasks /Run /TN "ETA-Diamond-PropDrawdownGuardEvery15Min"
schtasks /Run /TN "ETA-Diamond-LeaderboardHourly"

# 3. Run the current launch gate
python -m eta_engine.scripts.prop_launch_check --json

# 4. Optional: inspect the separate futures prop-ladder dry-run lane
python -m eta_engine.scripts.prop_live_readiness_gate --json

# 5. Only if the launch gate returns GO is the supervisor free to trade prop again
```

---

## Diagnostics — first 5 things to check on any incident

1. `python -m eta_engine.scripts.diamond_ops_dashboard` — unified status across all audits
2. `python -m eta_engine.scripts.diamond_prop_drawdown_guard` — current signal + per-rule status
3. `Get-Content C:\EvolutionaryTradingAlgo\logs\eta_engine\alerts_log.jsonl | Select-Object -Last 20` — recent alerts
4. `Get-Content C:\EvolutionaryTradingAlgo\var\eta_engine\state\diamond_prop_drawdown_guard_latest.json` — latest receipt
5. `python -m eta_engine.scripts.prop_launch_check --json` — current Diamond/Wave-25 launch verdict

---

## Known healthy-state baselines

If the system shows these AND the prop-fund account looks stable, you're fine:

- `prop_halt_active.flag` does NOT exist
- `prop_watch_active.flag` does NOT exist
- Drawdown guard signal: `OK`
- Launch readiness: `GO` from `python -m eta_engine.scripts.prop_launch_check --json`
- Daily PnL within ±$1500 (3% of $50K)
- Total PnL within -$2500 to +$3000 envelope

---

## Contacts

- Operator: edward.t.avila@gmail.com
- IBKR support: 1-877-442-2757 (US)
- Prop firm: see operator's signed eval agreement

---

## Hard rules (DO NOT violate)

1. **NEVER** disable the drawdown guard while live positions are open without explicitly flattening first.
2. **NEVER** modify `RETIREMENT_THRESHOLDS_USD` or `RETIREMENT_THRESHOLDS_R` while live — that's a Friday-EOD-only change.
3. **NEVER** bump `PROP_READY_CAPITAL_PER_BOT` more than 2x per week.
4. **NEVER** add a bot to `DIAMOND_BOTS` to "rescue" a strategy mid-incident.
5. **ALWAYS** create a post-mortem in `docs/POSTMORTEM_<date>.md` after any L3+ incident.
