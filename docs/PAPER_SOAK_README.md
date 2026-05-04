# Paper-Soak Realism Suite — Pre-Live-Money Workflow

**Read this BEFORE running any backtest.  Read it AGAIN before every live cutover.**

This directory documents the realistic-fill paper-soak suite — the
authoritative pre-live evaluation pipeline for every ETA strategy
across every ticker we trade.  The legacy paper-soak inflated PnL by
multiple stacked bugs (4× MNQ multiplier, 30× session replay,
volume_profile look-ahead, wrong-side stops, no commissions, no
slippage).  The current suite fixes all of those and adds hard rails
that refuse to score malformed signals.

## What lives where

| Module                                  | Purpose                                                     |
| --------------------------------------- | ----------------------------------------------------------- |
| `feeds/instrument_specs.py`             | Verified CME / spot specs (tick, point_value, commission)   |
| `feeds/realistic_fill_sim.py`           | Realistic fill model (slip, commission, straddle resolver)  |
| `feeds/signal_validator.py`             | Hard rules: stop-side, RR sanity, notional cap              |
| `backtest/engine.py` — `_Open`          | `__post_init__` invariant (kills wrong-side bugs at source) |
| `scripts/paper_trade_sim.py`            | Per-bot sim with walk-forward IS/OOS                        |
| `scripts/paper_soak_tracker.py`         | Sequential / parallel soak runs (with dup-window guard)     |
| `scripts/fleet_realism_audit.py`        | One command, full fleet, surfaces invalid signals + gap     |
| `scripts/strategy_optimizer.py`         | Parallel grid search with deflated Sharpe                   |
| `scripts/strategy_creation_harness.py`  | New-strategy gate (5-light elite check + random baseline)   |

## The three sim modes

| Mode          | Use when                                  | What it represents                          |
| ------------- | ----------------------------------------- | ------------------------------------------- |
| `realistic`   | Default for everything                    | Best estimate of live PnL — slip + comm     |
| `pessimistic` | Stress test; what's the floor             | Live PnL in poor liquidity / wide spreads   |
| `legacy`      | A/B reference only — DO NOT DECIDE WITH IT | Old frictionless behaviour (perfect fills)  |

Trust `realistic` for go/no-go decisions.  `legacy` exists only to
quantify the realism gap.  If `pessimistic` shows a strategy is still
profitable, the live result is likely close to that floor.

## The five-light elite gate (creation harness)

A new strategy must clear ALL FIVE lights before paper-soak begins:

1. **Signal validity** — zero rejected signals (no wrong-side stops)
2. **Sample size** — ≥ 30 trades on the held-out window
3. **OOS profitability** — net PnL > 0 on the held-out window
4. **OOS decay** — IS-vs-OOS PnL drop < 50%
5. **Beats baseline** — OOS PnL ≥ 1.5× a random-entry baseline

Yellow on any light = needs more work.  Red = strategy bug; do not
paper-soak.  Run the harness with `--random-baseline` to populate
light #5.

## The pre-live checklist

Before any strategy gets real-money capital:

```bash
# 1. Full fleet realism audit — surfaces every invalid signal in the fleet
python -m eta_engine.scripts.fleet_realism_audit --workers 8 --strict

# 2. Per-strategy walk-forward (single-bot detail)
python -m eta_engine.scripts.paper_trade_sim \
    --bot <bot_id> --days 90 --walk-forward --mode realistic

# 3. Pessimistic stress — strategy must remain profitable here too
python -m eta_engine.scripts.paper_trade_sim \
    --bot <bot_id> --days 90 --mode pessimistic

# 4. Optimizer regression — confirm chosen parameters still rank in the
#    top 5 of a fresh grid search (if not, parameters drifted)
python -m eta_engine.scripts.strategy_optimizer \
    --kind <kind> --symbol <SYM> --timeframe <TF> \
    --grid <param=v1,v2,v3 ...> --workers 4

# 5. All tests pass
python -m pytest eta_engine/tests/test_realistic_fill_sim.py \
    eta_engine/tests/test_signal_validator.py
```

If any of the above fails or shows red, **the strategy is not live-ready**.

## Falsification criteria (pre-commit before live)

For each strategy you intend to deploy, write down — BEFORE going live
— numeric criteria that would make you turn the strategy off.  Examples:

- "I will turn this off if WR drops below 50% over a rolling 30-trade window."
- "I will turn this off if 30-day net PnL is below -2% of starting equity."
- "I will turn this off if OOS Sharpe drops below 0.5 in any monthly recheck."

Without falsification criteria you have a belief, not a strategy.

## Bugs that the suite catches by construction

The following ALL killed paper-soak credibility before the rebuild.
Each is now caught either at signal time (`signal_validator`), at
position construction (`_Open.__post_init__`), or at session
recording (`paper_soak_tracker` dup detector).

| Bug                                                  | Where caught                                |
| ---------------------------------------------------- | ------------------------------------------- |
| Wrong-side stop (volume_profile, vwap_reversion)     | `_Open.__post_init__` + `signal_validator`  |
| Wrong-side target                                    | `_Open.__post_init__` + `signal_validator`  |
| RR > 50 or < 0.1                                     | `signal_validator`                          |
| Stop > 20% from entry (frozen-profile drift)         | `signal_validator`                          |
| Notional > 5× equity (degenerate qty sizing)         | `signal_validator`                          |
| Same-bar straddle deterministically picking stop     | `realistic_fill_sim` straddle resolver      |
| Zero entry slippage                                  | `realistic_fill_sim`                        |
| Zero stop slippage                                   | `realistic_fill_sim`                        |
| Zero commissions                                     | `realistic_fill_sim`                        |
| Volume profile look-ahead                            | `volume_profile_strategy.py` try/finally    |
| Same-window session replay summing                   | `paper_soak_tracker` dup detector           |
| Wrong MNQ point value (0.50 / tick vs 2.00 / point)  | `instrument_specs.py`                       |
| qty never propagated into PnL                        | `paper_trade_sim` recompute from risk %     |
| Inverted funding-rate pullback filter                | Fixed in `funding_rate_strategy.py`         |
| Ensemble-averaged stop on wrong side                 | Fixed + guarded in `ensemble_voting`        |

## What the suite does NOT cover (live-path work still required)

These were called out by the risk-execution audit and are the next
priority before real money flows:

1. `signal_validator` is wired into the SIM only, not the live mnq_bot
   `on_signal` path — wire it before live.
2. `venues/ibkr_live.py` reads `request.quantity` but the field is
   `request.qty` — position cap is silently bypassed.  **STOP-LIVE-MONEY**
3. `venues/ibkr_live.py:place_order` submits a naked market order, no
   bracket attached — a process crash leaves an unprotected position
   at the broker.  **STOP-LIVE-MONEY**
4. `MnqLiveSupervisor.start()` does not call `get_positions()` to
   reconcile broker truth before resuming signals.  **STOP-LIVE-MONEY**
5. Daily-loss limit checks realized-only; intraday unrealized excursion
   can pierce the Apex trailing-DD line silently.

These are not paper-soak bugs.  They are live-path bugs.  Treat them
as P0 before any capital.

## How to add a new instrument

1. Verify the CME / venue contract spec (tick, point value, commission).
2. Add an `InstrumentSpec` entry to `feeds/instrument_specs.py`.
3. Add the symbol to the bot's registry entry in `per_bot_registry.py`.
4. Run `fleet_realism_audit` to confirm the new symbol resolves.

## How to evaluate a new strategy

1. Implement the strategy_kind in the appropriate `*_strategy.py` file.
2. Register a candidate bot in `per_bot_registry.py` with
   `promotion_status="creation_test"`.
3. Run `strategy_creation_harness --bot <bot_id> --random-baseline`
4. Iterate until ALL FIVE lights are green.
5. Then paper-soak with `paper_soak_tracker --days 30`.
6. After 30+ unique-window sessions, run `fleet_realism_audit --strict`.
7. If audit passes and falsification criteria are written, candidate
   is ready for live consideration.

## Memory: what NOT to do

- Do not run paper_soak_tracker repeatedly with the same `--days` —
  the tracker now refuses duplicates, but accumulating "session counts"
  by replaying the same window is a process error, not a feature.
- Do not promote a strategy with N < 30 OOS trades.  vwap_mr_btc at
  N=2 is an example of how this previously masqueraded as an edge.
- Do not trust `legacy` mode for any decision.  It exists only to
  quantify how unrealistic the previous evaluation pipeline was.
- Do not bypass `_Open.__post_init__` — if you find yourself wanting
  to disable it, you have a strategy bug.  Fix the strategy.
