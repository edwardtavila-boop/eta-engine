# Research Log — 2026-04-26 — Post-rebrand baseline

**Purpose:** Establish a clean baseline of framework behavior after the
`apex_predator → eta_engine` rebrand. Verify research workflows still
produce expected output and that gating logic remains intact.

This is a **process checkpoint, not an edge claim.** Synthetic demo
data, not real markets. The interesting result is that the strict
gate correctly *failed* a strategy that looked good on aggregate but
degraded heavily out-of-sample.

---

## Backtest demo — `python -m eta_engine.scripts.run_backtest_demo`

| Metric | Value |
|---|---|
| Total trades | 36 |
| `<-1R` outcomes | 12 |
| `+1..2R` outcomes | 17 |
| `>2R` outcomes | 0 |
| Max drawdown | 2.97% |
| Confluence range | 7.0–8.0 (no 8.0+ trades) |

**Observations**
- No `>2R` trades in the sample — either the synthetic data has no
  trends long enough to reach the 2R target, or the exit logic is
  capping winners at 2R. Worth investigating in a real-data run.
- **Regime tags not attached to trades.** Output says `_regime tags
  not attached to trades in this run._` — a known feature gap. If
  regime conditioning is supposed to be a key edge driver, the
  reporting pipeline should surface it.
- **Concentration in low confluence bands** (7.0–8.0). No trades
  scored above 8.0. Either threshold is set too low for high-quality
  fires, or the synthetic data doesn't generate strong signals.

## Walk-forward demo — `python -m eta_engine.scripts.run_walk_forward_demo`

5 anchored windows, 2880 bars total.

| # | IS Sharpe | OOS Sharpe | IS trades | OOS trades | OOS ret % | Degradation % | DSR |
|---|---|---|---|---|---|---|---|
| 0 | 26.90 | 8.87 | 32 | 16 | 10.96 | **67.0** | 1.000 |
| 1 | 19.83 | 4.60 | 68 | 13 | 4.80 | **76.8** | 1.000 |
| 2 | 16.48 | 19.95 | 98 | 15 | 17.95 | 0.0 | 1.000 |
| 3 | 18.92 | 24.32 | 133 | 13 | 16.62 | 0.0 | 0.997 |
| 4 | 18.52 | 11.43 | 167 | 16 | 13.52 | **38.3** | 1.000 |

| Aggregate | Value |
|---|---|
| IS Sharpe | 20.13 |
| OOS Sharpe | 13.83 |
| OOS degradation (avg) | 36.43% |
| Deflated Sharpe (DSR) | 1.000 |
| Per-fold DSR median | 1.000 |
| Per-fold DSR pass fraction | 100% (threshold 50%) |
| **Strict gate verdict** | **FAIL** |

**Why the gate failed (correctly)**
The DSR-based aggregate looks pristine (1.0). But the strict gate
also enforces *per-window OOS degradation* — and 3 of 5 windows
(0, 1, 4) have degradation > 35%. A strategy whose OOS Sharpe drops
67% and 77% in two windows is overfit, regardless of the headline
DSR. The gate is doing exactly what it should.

This is the kind of result that, in a real research workflow, would
either trigger a regularization pass on the strategy or reject it
outright. The framework is correctly *not* promoting an apparently
strong strategy that fails the per-fold consistency check.

---

## What this validates post-rebrand

- [x] `eta_engine.backtest.walk_forward` imports + runs end to end
- [x] `eta_engine.scripts.run_backtest_demo` produces well-formed
      markdown report
- [x] `eta_engine.scripts.run_walk_forward_demo` runs the full
      strict-gate pipeline (DSR, degradation, median, pass-fraction)
- [x] Header text says "EVOLUTIONARY TRADING ALGO" — branding propagated
- [x] Trade-counting, Sharpe calc, DSR calc all return non-NaN values

## Open research items surfaced

1. **Regime tagging in trade output.** Demo report shows `_regime
   tags not attached to trades in this run._` — if regime is a
   first-class feature in the framework, the reporting pipeline
   should populate this from the regime classifier on every trade.
2. **No `>2R` trades in 36-trade sample.** Investigate exit logic
   on a real-data run; rule out an exit cap.
3. **Strict gate FAIL on the demo strategy is the correct outcome.**
   But it would be useful to have a "diagnostic" mode that surfaces
   *why* a strategy fails — currently the operator has to read the
   table and infer it (degradation > 35% in 3 windows).

## Next research session candidates

- Replace synthetic demo data with the local parquet bars from
  `C:\mnq_data\` and re-run walk-forward on real MNQ history.
- Run the same walk-forward on `bots/btc_hybrid` once Coinbase/Binance
  keys are in place.
- Add a `degradation_breakdown` field to walk-forward output so the
  failure mode is explicit, not inferred from the table.
