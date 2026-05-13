# Monday Morning Operator Runbook

**Target launch:** Monday 2026-05-18 — BluSky 50K eval, conditional-routing architecture (wave-25)

**This is your bedside guide.** Print it. Have your phone open to Telegram. Open the VPS dashboard. Read every section before you start.

---

## TL;DR — the only thing you need to remember

```powershell
ssh forex-vps "cd C:\EvolutionaryTradingAlgo\eta_engine && python -m eta_engine.scripts.prop_launch_check"
```

Run this. Read the **Action items**. Do them in order. Re-run. Done.

If the verdict is `GO`, you're cleared. If it's `HOLD` or `NO_GO`, the script tells you exactly what's still in the way.

---

## Sunday EOD checklist (do before bed Sunday)

- [ ] **Run the launch check.** `python -m eta_engine.scripts.prop_launch_check` on VPS. Read the verdict.
- [ ] **Set Telegram alert channel** if not already (see § Telegram one-time setup below). Verify with `verify_telegram --send-test`.
- [ ] **Decide which bot(s) go EVAL_LIVE.** Recommended starting position: `mnq_futures_sage` only (it's the rank-1 PROP_READY bot with n=109, +1.26R avg, 64% WR on real paper data). Run:
  ```powershell
  python -m eta_engine.scripts.manage_lifecycle set mnq_futures_sage EVAL_LIVE
  ```
- [ ] **Confirm 2 PROP_READY bots designated.** If still only 1 in the leaderboard, you can either (a) wait one more day or (b) accept DEGRADED mode and launch with one bot.
- [ ] **Final dry-run before bed.** Verdict should be GO or HOLD (HOLD is acceptable if the only blocker is "need 1 more PROP_READY designation"). NO_GO means do NOT launch.

---

## Monday morning sequence

### 08:00 ET — wake up, coffee, validate

```powershell
ssh forex-vps "cd C:\EvolutionaryTradingAlgo\eta_engine && python -m eta_engine.scripts.prop_launch_check"
```

Verify:
- `drawdown_guard.signal = OK` (no overnight HALT)
- `wave25_lifecycle.EVAL_LIVE >= 1`
- `alert_channels` shows telegram configured
- `freshness` is GO (cron tasks running)

If anything looks off, **DO NOT** intervene yet — read the action items first. The system is designed to be honest about its state.

### 09:00 ET — open BluSky / Tradovate platform

- Log into the BluSky platform UI (operator-side; ETA Tradovate routing remains DORMANT per project policy)
- Open the **Positions** panel
- Confirm starting balance = $50,000
- Note: ETA does not place orders on Tradovate. Bot orders flow through IBKR per the routing matrix.

### 09:30 ET — RTH open, first bar

- The supervisor's wave-25 gate evaluates every signal:
  - `EVAL_LIVE` bots with safe prospective loss → live broker
  - `EVAL_PAPER` bots → shadow signal log (no live order)
  - Soft-DD trip → fallback to paper
  - HALT signal → reject entirely
- Watch the supervisor heartbeat for entries: `Get-Content $env:ETA_HEARTBEAT_PATH | ConvertFrom-Json`

### 09:31-10:30 ET — watch first 3-5 fills

Stay at the desk. Do not multitask. After the first 3-5 fills you have a posture:
- Stops are landing where the strategy expected
- Slippage looks reasonable
- The drawdown buffer is moving as expected
- Telegram is actually receiving HALT/WATCH alerts (or you wouldn't know they fired)

### 10:30 ET onward — check at top of every hour

- `python -m eta_engine.scripts.prop_launch_check` (read drawdown buffer, watch the action items shrink)
- Glance at the BluSky platform positions
- If the drawdown_guard shifts to WATCH, the supervisor is auto-halving sizes — let it run. If HALT, the supervisor refuses entries — do NOT manually override.

### 12:00 ET — lunch check

Daily PnL check. If today's PnL is approaching -$750 (50% of the $1500 daily DD limit), the wave-25 risk gate is already routing new signals to paper. Don't intervene.

### 15:30 ET — pre-close consistency check

Look at today's PnL ratio vs week-to-date. If a single day is approaching 30% of the eval's total profit, you're heading toward a consistency-rule violation. Stop adding contracts.

### 16:30 ET — EOD review

```powershell
python -m eta_engine.scripts.prop_launch_check
```

- Daily buffer reset for tomorrow at 00:00 UTC (8 PM ET; 7 PM CT)
- Static buffer carries (high-water-mark relative)
- Note any shadow signals (`route_paper:` rejections) — those are signals the bots wanted to take but the gate routed to paper. Useful kaizen data for tomorrow.

---

## Severity ladder — when things go wrong

Always run `prop_launch_check` first to see WHICH gate fired. Reasons:

| Reason prefix | Meaning | Operator action |
|---|---|---|
| `gate_reject: lifecycle_retired` | Bot retired; refused all signals | None — that's the design |
| `gate_reject: prop_guard_halt` | Drawdown guard HALT | See `docs/PROP_FUND_ROLLBACK_RUNBOOK.md` |
| `gate_reject: would_breach_daily_dd` | Single trade would exceed daily DD buffer | Acceptable — that's why the gate exists |
| `gate_reject: would_breach_static_dd` | Single trade would exceed static DD | Acceptable — same |
| `route_paper: lifecycle_eval_paper` | Bot not opted into live | Acceptable — operator chose paper-only |
| `route_paper: would_breach_soft_dd` | Trade exceeds 50% of daily buffer | Acceptable — wave-25 cautious routing |
| `gate_size_collapsed: <signal>` | WATCH multiplier dropped size to 0 | Acceptable — too risky to enter |

**If you see a reject reason NOT in this table:** new code path. Log it and check `docs/WAVE25_PROP_LAUNCH_OPS.md` for the latest matrix.

---

## Telegram one-time setup (if not done yet)

1. **Create the bot** on your phone:
   - Open Telegram, message `@BotFather`
   - Send `/newbot`, follow prompts
   - **Save the bot token** (looks like `123456:AAH...`)

2. **Find your chat ID:**
   - Send any message to your new bot
   - Open `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` in browser
   - Look for `"chat":{"id":NNNNNN,...}`

3. **Set env vars on VPS:**
   ```powershell
   ssh forex-vps
   [Environment]::SetEnvironmentVariable("ETA_TELEGRAM_BOT_TOKEN", "<token>", "Machine")
   [Environment]::SetEnvironmentVariable("ETA_TELEGRAM_CHAT_ID", "<chat_id>", "Machine")
   exit
   ```

4. **Verify:**
   ```powershell
   ssh forex-vps "cd C:\EvolutionaryTradingAlgo\eta_engine && python -m eta_engine.scripts.verify_telegram --send-test"
   ```

   You should receive a Telegram message from your bot within ~5 seconds. If you don't, the env vars aren't taking effect — check that the VPS supervisor task was restarted to pick up the new environment.

---

## Lifecycle CLI cheat sheet

```powershell
# List current state of all bots
python -m eta_engine.scripts.manage_lifecycle list

# Promote a bot to live (the Monday opt-in)
python -m eta_engine.scripts.manage_lifecycle set mnq_futures_sage EVAL_LIVE

# Park a bot in paper-only
python -m eta_engine.scripts.manage_lifecycle set mes_sweep_reclaim_v2 EVAL_PAPER

# Retire a bot (refuse all signals)
python -m eta_engine.scripts.manage_lifecycle set foo_bot RETIRED

# Revert to default (EVAL_PAPER)
python -m eta_engine.scripts.manage_lifecycle clear mnq_futures_sage
```

The CLI is idempotent — re-running with the same state is a no-op. Atomic writes mean a crash mid-set can't corrupt the JSON.

---

## Recovery / rollback

If the eval account is at risk:

1. **First action: STOP NEW ENTRIES.** Set all bots to RETIRED:
   ```powershell
   for $b in (python -m eta_engine.scripts.manage_lifecycle list --json | jq -r '.bots[].id'):
       python -m eta_engine.scripts.manage_lifecycle set $b RETIRED
   ```
   Or just set the prop_halt flag manually:
   ```powershell
   $payload = @{ts=(Get-Date -Format "o"); rationale="operator_manual_halt"; prop_ready_bots=@()} | ConvertTo-Json
   $payload | Out-File "C:\EvolutionaryTradingAlgo\var\eta_engine\state\prop_halt_active.flag" -Encoding utf8
   ```

2. **Second action: flatten via BluSky UI** (not via bot — the bot can't reach the broker if HALT is set). See `docs/PROP_FUND_ROLLBACK_RUNBOOK.md` § B for the manual flatten procedure.

3. **Third action: post-mortem.** Write `docs/POSTMORTEM_<date>.md`. The system already logs to `alerts_log.jsonl` and `shadow_signals.jsonl` — use that data to reconstruct what happened.

---

## What the system does on its own (no operator action needed)

- **Every 15 min:** drawdown guard recomputes; alert dispatcher pushes HALT/WATCH to configured channels; ledger refreshes; launch readiness re-evaluates
- **Every hour:** leaderboard recomputes PROP_READY designations; ops dashboard refreshes; feed sanity audit runs; wave-25 status snapshot updates; prop allocator recomputes capital allocation
- **Daily 11:00 ET:** promotion gate, demotion gate, sizing audit, direction stratify, watchdog
- **Weekly Sunday 11:00 ET:** authenticity audit, CPCV runner, regime stratify, preset validator, sanitizer (read-only audits)

Trust the cron. If a receipt is stale, `prop_launch_check.freshness` will flag it. Otherwise the system is doing what it's supposed to.

---

## Contacts

- BluSky support: see operator's signed eval agreement
- IBKR support: 1-877-442-2757 (US)
- Project memory: `~/.claude/projects/.../memory/MEMORY.md`
