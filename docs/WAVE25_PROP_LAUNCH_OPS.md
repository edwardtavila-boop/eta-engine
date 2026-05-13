# Wave-25 Prop Launch Ops Guide

**Last updated:** 2026-05-13 (wave-25)
**Target launch:** Monday 2026-05-18 — BluSky 50K eval, conditional-routing architecture

---

## What wave-25 changed

The prop launch design is now **conditional routing**: every trade signal is
evaluated against live drawdown buffers + per-bot lifecycle state and routed
to one of three destinations:

| Target | When | What happens |
|---|---|---|
| `live` | Bot is `EVAL_LIVE` or `FUNDED_LIVE` AND prospective loss is safe | Submit to broker |
| `paper` | Bot is `EVAL_PAPER` OR prospective loss trips soft DD threshold | Skip broker (currently dropped; future: paper-trading sim) |
| `reject` | Bot is `RETIRED`, prop guard HALT, OR loss would breach hard DD/static DD | Refuse signal entirely |

The composite gate lives in
`eta_engine/feeds/capital_allocator.py::resolve_execution_target` and is
called by `jarvis_strategy_supervisor._maybe_enter` immediately after the
existing wave-22 prop guard.

## Per-bot lifecycle state

State lives in `var/eta_engine/state/bot_lifecycle.json`:

```json
{
  "bots": {
    "m2k_sweep_reclaim": "EVAL_LIVE",
    "met_sweep_reclaim": "EVAL_PAPER",
    "mes_sweep_reclaim_v2": "EVAL_PAPER"
  }
}
```

Defaults: any bot **not** explicitly listed defaults to `EVAL_PAPER` —
**conservative**. Operator must opt bots into `EVAL_LIVE` explicitly.

CLI helpers (Python REPL):

```python
from eta_engine.feeds.capital_allocator import (
    set_bot_lifecycle,
    LIFECYCLE_EVAL_LIVE,
    LIFECYCLE_EVAL_PAPER,
    LIFECYCLE_FUNDED_LIVE,
    LIFECYCLE_RETIRED,
)

# Promote m2k to live eval trading
set_bot_lifecycle("m2k_sweep_reclaim", LIFECYCLE_EVAL_LIVE)

# Park mes_v2 in paper-only (R-vs-USD bug not yet reconciled)
set_bot_lifecycle("mes_sweep_reclaim_v2", LIFECYCLE_EVAL_PAPER)
```

## Pre-trade risk gate

Triggered for every PROP_READY signal. Reads buffers from
`var/eta_engine/state/diamond_prop_drawdown_guard_latest.json`:

- **`reject`** — prospective loss ≥ daily DD buffer OR static DD buffer
- **`route_to_paper`** — prospective loss ≥ 50% of daily DD limit (soft threshold)
- **`allow_live`** — safe

For the BluSky 50K eval, daily DD = $1,500. Soft threshold = $750.
Default prospective loss estimate per signal = **$250** (0.5% of $50K).

## Data-source tagging — IMPORTANT

Wave-25 introduced `data_source` tagging on every trade-close record:

- `live` — real fills from broker
- `paper` — paper-trading sim
- `backtest` — historical replay
- `historical_unverified` — legacy archive (excluded from production audits)
- `live_unverified` — canonical path, no explicit tag (excluded)
- `test_fixture` — known test bots (`t1`, `propagate_bot`) (excluded)

All audits (`diamond_leaderboard`, `diamond_promotion_gate`,
`diamond_sizing_audit`, `diamond_direction_stratify`,
`diamond_demotion_gate`, `diamond_feed_sanity_audit`,
`diamond_prop_drawdown_guard`) now default to `live + paper` only.

**Effect**: until live trades accumulate, all audits show `n_trades=0` per
bot. This is correct — there is no live evidence yet. Going forward, every
supervisor-emitted close is tagged automatically based on bot's
`execution_mode` attribute (defaults to `live`).

## Telegram alert channel setup (REQUIRED before Monday)

The alert dispatcher (wave-24) needs Telegram credentials to push
HALT/WATCH alerts when the dashboard isn't being watched.

### One-time setup
1. Open Telegram on your phone, message `@BotFather`
2. Send `/newbot`, follow prompts to create a bot. Save the bot token
   (looks like `123456:AAH...`)
3. Find your chat ID:
   - Message your new bot any text
   - Open `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
   - Look for `"chat":{"id":NNNNNN,...}`
4. Set environment variables on the VPS (Machine scope, persists across reboots):

```powershell
[Environment]::SetEnvironmentVariable("ETA_TELEGRAM_BOT_TOKEN", "123456:AAH...", "Machine")
[Environment]::SetEnvironmentVariable("ETA_TELEGRAM_CHAT_ID", "999999999", "Machine")
```

5. Verify by manually triggering the dispatcher:

```powershell
ssh forex-vps "cd C:\EvolutionaryTradingAlgo\eta_engine && python -m eta_engine.scripts.diamond_prop_alert_dispatcher --dry-run"
```

Look for `channels=(telegram)` instead of `channels=(none configured)`.

### Optional: Discord
Same idea, but set `ETA_DISCORD_WEBHOOK_URL` instead. Generic
Slack-compatible webhook also supported via `ETA_GENERIC_WEBHOOK_URL`.

## Expected pre-launch state (the chicken-and-egg)

**Until the first live trade lands, the prelaunch dryrun WILL say NO_GO.**
This is correct safety behavior — the leaderboard requires `n_trades >= 100`
+ `avg_r >= +0.20R` of *live + paper* evidence before designating
PROP_READY. Until Monday open, that threshold is not met.

The operator's bootstrap procedure:

1. Decide which bots to launch live: edit lifecycle state per the
   recommendations in the launch checklist below
2. Accept that the dryrun returns NO_GO. The override is your
   `set_bot_lifecycle()` call combined with operator judgment that
   the wave-25 conditional routing + soft DD threshold + manual
   monitoring is sufficient compensating control
3. Open the BluSky/Tradovate platform in front of you for Monday morning
   as an operator-side view only; ETA Tradovate routing remains DORMANT
   until code and docs are explicitly reactivated together
4. Watch the first 3-5 m2k trades manually before stepping away
5. Once ~100 live trades accumulate (could take 2-3 sessions), the
   leaderboard auto-promotes m2k to PROP_READY and the dryrun flips
   to GO without intervention

The system is *correctly* refusing to auto-GO without evidence. Wave-25's
job is to make that refusal accurate and actionable, not to override it.

## Monday launch checklist

Run from operator's local workstation; the bullets are operator-side acks:

- [ ] Operator decision: which bots are EVAL_LIVE vs EVAL_PAPER for Monday open
  - Recommended: m2k=EVAL_LIVE, met=EVAL_PAPER, mes_v2=EVAL_PAPER
  - Rationale: m2k has 70% WR / 0.46 avg R / strongest CPCV; met has 1-day sample; mes_v2 has R-vs-USD translation bug unreconciled
- [ ] `set_bot_lifecycle` called on each bot per the decision above
- [ ] Telegram credentials set on VPS (`ETA_TELEGRAM_BOT_TOKEN`, `ETA_TELEGRAM_CHAT_ID`)
- [ ] Test alert sent manually via dispatcher (verify operator receives it)
- [ ] BluSky/Tradovate sub-account credentials confirmed for operator-side
  visibility only; ETA Tradovate routing remains DORMANT until explicit
  reactivation
- [ ] Pre-launch dryrun = GO (`python -m eta_engine.scripts.diamond_prop_prelaunch_dryrun`)
- [ ] First-day plan documented: which time window does the operator monitor?

## Post-launch monitoring (continuous)

Daily cron tasks already running on VPS:

- ETA-Diamond-LedgerEvery15Min — refreshes closed_trade_ledger
- ETA-Diamond-PropDrawdownGuardEvery15Min — HALT/WATCH/OK signal
- ETA-Diamond-PropAlertDispatcherEvery15Min — pushes to Telegram (once configured)
- ETA-Diamond-LeaderboardHourly — composite scoring + PROP_READY designation
- ETA-Diamond-OpsDashboardHourly — unified status surface

Operator should glance at the dashboard at:
- Pre-open (8:30 ET): verify GO from prelaunch dryrun
- Mid-day (12:00 ET): check daily PnL vs DD buffer
- Pre-close (15:30 ET): consistency-rule sanity check
- EOD (16:30 ET): commit decisions for next session
