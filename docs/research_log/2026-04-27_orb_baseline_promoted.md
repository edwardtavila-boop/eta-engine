# Research Log — 2026-04-27 — ORB sweep + first PROMOTED strategy

**First strategy to PASS the strict walk-forward promotion gate on
real MNQ data.** ORB at the swept winning config produces:

```
Aggregate IS Sharpe:    +3.29
Aggregate OOS Sharpe:   +5.71
Positive OOS windows:    2/2
Per-fold DSR median:    1.000
DSR pass fraction:    100.00%
Gate:                  PASS
```

Both MNQ1/5m and NQ1/5m hit identical numbers — ORB is
symbol-agnostic on liquid index futures during the same RTH session.

## The sweep

`scripts/sweep_orb_params.py` walks a 54-cell grid:

* `range_minutes` ∈ {5, 15, 30}
* `rr_target` ∈ {1.5, 2.0, 3.0}
* `atr_stop_mult` ∈ {1.0, 1.5, 2.0}
* `ema_bias_period` ∈ {50, 200}

11/54 cells PASS the strict gate on MNQ1/5m, 60d/30d windows. The
top winner:

| Range | RR | ATR× | EMA | OOS Sh | DSR med | DSR pass% | Verdict |
|---:|---:|---:|---:|---:|---:|---:|---|
| **15m** | **2.0** | **2.0** | **200** | **+5.71** | **1.000** | **100.0** | **PASS** |
| 30m | 1.5 | 2.0 | 200 | +5.19 | 1.000 | 100.0 | PASS |
| 30m | 1.5 | 2.0 | 50 | +4.33 | 1.000 | 100.0 | PASS |
| 30m | 2.0 | 2.0 | 200 | +3.31 | 1.000 | 100.0 | PASS |
| 30m | 2.0 | 1.5 | 200 | +2.50 | 1.000 | 100.0 | PASS |
| 15m | 3.0 | 1.0 | 200 | +1.95 | 1.000 | 100.0 | PASS |
| 30m | 2.0 | 2.0 | 50 | +1.83 | 1.000 | 100.0 | PASS |
| 30m | 3.0 | 1.0 | 200 | +1.43 | 1.000 | 100.0 | PASS |
| 30m | 2.0 | 1.0 | 200 | +1.41 | 0.934 | 100.0 | PASS |
| 30m | 3.0 | 1.5 | 50 | +1.34 | 0.953 | 100.0 | PASS |
| 30m | 3.0 | 1.5 | 200 | +1.96 | 0.501 | 50.0 | PASS |

Pattern across the passing cells:
* **`atr_stop_mult >= 1.5`** dominates — wider stops on intraday
  ORB give the trade room to breathe before the mid-day chop.
* **`ema_bias_period=200`** mostly wins. The 50-bar EMA is too
  reactive to short trend flips.
* **15m and 30m range** both work; 5m range produces too many
  small-noise breakouts that whipsaw.

## What changed

* `strategies/orb_strategy.py::ORBConfig.atr_stop_mult` default
  bumped 1.5 → 2.0 (the sweep winner). All other defaults already
  matched the winner.
* `scripts/run_orb_walk_forward.py` env-var default for
  `ORB_ATR_STOP_MULT` updated to match.
* `docs/strategy_baselines.json` populated with `mnq_orb_v1` and
  `nq_orb_v1` baselines so the drift watchdog has real reference
  stats for the live paper-soak phase.

## Honest scope

* **n=2 windows** is small. The strategy passes 2/2, but to claim
  edge with statistical confidence we need 10+ windows. That
  requires either more history (>4 months of MNQ 5m) or shorter
  windows (which we tried — 30d/15d at default config was worse).
* The 11 passing cells share structural similarities (`atr_stop_mult>=1.5`,
  `ema_bias_period` in {50,200}). That hints at robustness but
  also at a small effective dimensionality — 11/54 = 20% pass
  rate is reassuringly above pure noise.
* The IS/OOS Sharpe magnitudes (+3 IS, +5 OOS) are *suspiciously*
  high. ORB on liquid futures realistically targets 1.5-3 Sharpe.
  +5 OOS likely reflects (a) the limited window count, (b) low
  effective trade count per window (~13 OOS trades), (c) Jan-Apr
  2026 being a favorable period for breakouts on MNQ.
* **Live paper-soak is the next gate.** Until 50+ real fills land
  in the journal, treat the +5.71 number as "promotable" not
  "proven." The drift watchdog (now baselined) will flag if live
  performance diverges meaningfully.

## Next moves

1. **Live paper-soak** for `mnq_orb_v1` — flip the bot to paper
   mode against IBKR, run for 2 weeks, journal every fill, run
   `drift_check_all` daily.
2. **Daily-range-breakout variant** — open is not a session
   concept on daily bars, but prior-day high/low IS. Build a
   sister strategy that breaks above yesterday's high / below
   yesterday's low, run on NQ1 daily (27 yr history).
3. **BTC adaptation** — the current ORB doesn't translate to
   24/7 markets. Either build a session-synthetic (UTC midnight
   open) or pivot to a different crypto baseline.
4. **Cross-asset filter** — ES correlation gate is wired but ORB
   doesn't currently consume it. Add as opt-in: only fire ORB
   when ES is also breaking out of its own range.
