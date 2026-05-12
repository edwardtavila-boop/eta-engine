# Futures-Only Fleet Pivot — 2026-05-12

**Operator decision (2026-05-12):** focus the fleet on futures + commodities
+ CME crypto futures.  Pause Alpaca spot crypto and any non-futures
trading lanes indefinitely.

This document is the master truth-surface for the pivot: what changed,
what to do next, and how to undo it.

---

## What is now ACTIVE

| Asset class | Symbols | Venue (primary) |
|---|---|---|
| Equity-index micros & futures | MNQ, NQ, MES, ES, MYM, M2K, RTY, YM | IBKR Pro · Tradovate (≤48h) · Tastytrade |
| Commodities | MCL, CL, GC, MGC, NG | IBKR Pro · Tradovate · Tastytrade |
| Forex futures | 6E, M6E | IBKR Pro |
| Treasury futures | ZN, ZB | IBKR Pro |
| **Crypto futures** (CME) | MBT, MET | IBKR Pro |

US-legal venue routing only.  Tradovate dormancy lifts when operator
credentials arrive (Step 5 of `BLUSKY_ONBOARDING_RUNBOOK.md`).

---

## What is CELLARED (deactivated, recoverable)

| Bot | Symbol | Venue | Deactivation file |
|---|---|---|---|
| `eth_sage_daily` | ETH (spot) | Alpaca/Coinbase | `per_bot_registry.py` line ~230 |
| `sol_optimized` | SOL (spot) | Alpaca | `per_bot_registry.py` line ~370 |

All other BTC / ETH / SOL / XRP spot bots were already cellared
(deactivated or `shadow_benchmark`) prior to this pivot.

### Re-activation procedure (per bot)

1. Open `eta_engine/strategies/per_bot_registry.py`
2. Find the `StrategyAssignment(bot_id="…")` entry
3. Inside `extras`, delete the three lines:
   ```python
   "deactivated": True,
   "deactivated_on": "2026-05-12",
   "deactivated_reason": (
       "Cellared 2026-05-12: operator pivot to futures-only fleet ..."
   ),
   ```
4. Commit + push + pull on VPS
5. Restart supervisor — the bot will rejoin the active set on its
   prior `promotion_status` (paper_soak / shadow / etc.)

---

## What is DORMANT (off until operator action)

| Item | Re-activation method |
|---|---|
| Tradovate venue | `ETA_TRADOVATE_ENABLED=1` env var + credential commit; see `BLUSKY_ONBOARDING_RUNBOOK.md` Step 5 |
| Spot crypto (Alpaca routing) | Per-bot cellar removal (above) — Alpaca adapter itself is intact |

---

## Venue precedence rules (active fleet)

The router picks the venue when the caller does not pin one:

```
preferred_futures_venue: ibkr   (default)
                        tastytrade (when IBKR is down + symbol supported)
                        tradovate  (when ETA_TRADOVATE_ENABLED=1)
```

Override per-bot in `bot_broker_routing.yaml` if you want a specific
strategy to prefer Tastytrade (e.g. for prop-firm separation).

---

## Tradovate 48h reactivation quick-card

When BluSky / Tradovate creds arrive:

```powershell
# 1. SSH to VPS
ssh forex-vps

# 2. Run the credential loader (prompts for 5 fields, stores in OS keyring)
cd C:\EvolutionaryTradingAlgo\eta_engine
python -m eta_engine.scripts.setup_tradovate_secrets

# 3. Verify OAuth
python -m eta_engine.scripts.authorize_tradovate --demo

# 4. Enable in env (paired with the routing-yaml commit — see Step 5
#    of BLUSKY_ONBOARDING_RUNBOOK.md for the un-dormancy commit pattern)
$env:ETA_TRADOVATE_ENABLED = "1"

# 5. Restart supervisor task so it picks up the env change
Stop-ScheduledTask -TaskName ETA-Supervisor
Start-ScheduledTask -TaskName ETA-Supervisor

# 6. Verify routing
python -m eta_engine.scripts.broker_routing_framework --verify-all
```

Then commit the dormancy lift:

```powershell
git add eta_engine/venues/router.py docs/dormancy_mandate.md
git commit -m "feat(tradovate): reactivate venue — BluSky 50K live"
git push
```

---

## Bracket-audit residue (2026-05-12)

Pre-pivot the IBKR paper account had 4 accumulated positions; 2 were
unprotected.  See deep-dive synthesis 2026-05-12 in session log.

Operator command to flatten paper-account residue (paper money — safe):

```powershell
# Flatten ALL paper-account positions
ssh forex-vps "C:\Program Files\Python312\python.exe" -m eta_engine.scripts.flatten_ibkr_positions --port 4002 --client-id 988 --confirm

# OR — surgical (only the unprotected ones)
ssh forex-vps "C:\Program Files\Python312\python.exe" -m eta_engine.scripts.flatten_ibkr_positions --port 4002 --client-id 988 --local-symbols MNQM6,MCLM6 --confirm
```

The `--local-symbols` filter was added 2026-05-12 specifically for this
case so the operator can keep managed positions (MYMM6, NGM26) while
flushing the rogue ones.

---

## Verification commands

```powershell
# Confirm 0 active spot bots
python -c "from eta_engine.strategies.per_bot_registry import ASSIGNMENTS; spot={'BTC','ETH','SOL','DOGE','AVAX','XRP'}; print('active spot bots:', sum(1 for a in ASSIGNMENTS if a.symbol.upper() in spot and not (a.extras or {}).get('deactivated', False)))"

# Cron fleet health
python -m eta_engine.scripts.l2_daily_summary

# Dashboard probe
python C:\Temp\probe_status.py  # see deep-dive synthesis
```

---

## Truth-surface cross-references

- `eta_engine/strategies/per_bot_registry.py` — cellaring source (2 deactivations)
- `eta_engine/venues/router.py` — `DORMANT_BROKERS` + `ETA_TRADOVATE_ENABLED`
- `eta_engine/docs/BLUSKY_ONBOARDING_RUNBOOK.md` — Tradovate reactivation step-by-step
- `eta_engine/docs/ROADMAP_JUNE_JULY_2026.md` — G5 multi-broker routing (Tradovate is here)
- `CLAUDE.md` hard rule #2 — active futures brokers list
