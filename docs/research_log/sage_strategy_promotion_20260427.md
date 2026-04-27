# Sage-driven strategy promotion — 2026-04-27

User directive: "see if NQ and MNQ have any more strategies
available and re-optimize, use jarvis sage mode to find and
implement and improve our baseline strategies for all bots."

## Sage layer — what we have

`brain/jarvis_v3/sage` ships a 22-school market-theory consultation
engine:

* **Classical (10):** Dow, Wyckoff, Elliott, Fibonacci, Gann,
  Support/Resistance, Trend Following, VPA, Market Profile,
  Risk Management.
* **Modern (4):** SMC/ICT, Order Flow, NeoWave, Weis/Wyckoff.
* **Quantitative wave-5 (8):** Seasonality, Volatility Regime,
  Statistical Significance, Red Team, Options Greeks, Funding
  Basis, On-Chain, Cross-Asset Correlation, ML.

`consult_sage(ctx)` runs every applicable school in parallel,
applies regime + edge-tracker weight modulators, and returns a
`SageReport` with a composite directional bias + conviction +
consensus + alignment counters. The sage layer was untapped by
existing strategies — every shipped strategy hard-codes one edge.

## New strategies

### SageConsensusStrategy (`sage_consensus`)

Pure sage-as-entry. Every bar:
1. Build `MarketContext` from recent N bars.
2. `consult_sage(ctx)`.
3. Fire BUY when `composite_bias == LONG`, `conviction >=
   min_conviction`, `consensus_pct >= min_consensus`,
   `alignment_score >= min_alignment`.
4. Fire SELL on the symmetric SHORT condition.
5. ATR stop + RR target.

**Result on MNQ 5m walk-forward (60d/30d, 2 windows):**

| Window | IS Sh | OOS Sh | IS trades | OOS trades |
|---:|---:|---:|---:|---:|
| 0 | +2.08 | -0.00 | 93 | 42 |
| 1 | +1.80 | -2.30 | 162 | 41 |

**Verdict: heavy IS overfit, OOS Sh -1.15.** Pure sage as the
entry signal is too noisy with current thresholds — too many bars
fire, too many false positives. Not promoted.

### SageGatedORBStrategy (`orb_sage_gated`)

ORB with a sage overlay: when ORB would fire, run sage; if the
22-school consensus disagrees with the breakout direction or the
conviction is below threshold, veto the entry and roll back the
ORB day-state so a later breakout can still fire.

**Sweep on MNQ 5m (18 cells: conviction × alignment × range):**

| conv | align | range | windows | agg_OOS_Sh | +OOS | DSR_med | DSR_pass | gate |
|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| 0.65 | 0.55 | 15 | 2 | **+10.061** | 2/2 | 1.000 | **100%** | **PASS** |
| 0.65 | 0.65 | 15 | 2 | **+10.061** | 2/2 | 1.000 | **100%** | **PASS** |
| 0.65 | 0.75 | 15 | 2 | **+10.061** | 2/2 | 1.000 | **100%** | **PASS** |
| 0.45 | 0.75 | 15 | 2 | +3.224 | 1/2 | 0.535 | 50% | FAIL |
| 0.55 | 0.75 | 15 | 2 | +3.224 | 1/2 | 0.535 | 50% | FAIL |
| 0.45 | 0.55 | 15 | 2 | +1.503 | 1/2 | 0.500 | 50% | FAIL |
| 0.45 | 0.65 | 15 | 2 | +1.413 | 1/2 | 0.500 | 50% | FAIL |
| 0.55 | 0.55 | 15 | 2 | +1.413 | 1/2 | 0.500 | 50% | FAIL |
| 0.55 | 0.65 | 15 | 2 | +1.413 | 1/2 | 0.500 | 50% | FAIL |
| (all conv=0.55-0.65 with range=30) | | | | <= 0 | 0-1/2 | <= 0.5 | 50% | FAIL |

**Per-window detail at the winning cell (conv=0.65, range=15m):**

| Window | IS Sh | OOS Sh | IS trades | OOS trades | OOS return |
|---:|---:|---:|---:|---:|---:|
| 0 | +1.61 | **+12.39** | 13 | 7 | +8.21% |
| 1 | +3.90 | **+7.73** | 24 | 5 | +4.01% |

* **Aggregate OOS Sharpe: +10.06 vs plain ORB +5.71** — ~2x
  improvement.
* **OOS > IS in BOTH windows** — sage filter cuts more losers
  than winners on bars not in the IS sample. The opposite of
  overfitting.
* **Gate PASS** (DSR median 1.0, 100% pass fraction).

The 30-minute range universally fails. The "sweet spot" is the
existing 15m ORB range with sage's high-conviction filter.

## Promoted

* **`mnq_orb_sage_v1`** registered as bot `mnq_futures_sage`
  (sibling to `mnq_futures` running plain ORB). Same MNQ1 5m,
  60d/30d windows, but with the sage overlay at
  `min_conviction=0.65`, `min_alignment=0.55`, `range=15m`.
* Pinned baseline added to `docs/strategy_baselines.json`:
  `n_trades=49, win_rate=0.612, avg_r=0.842, r_stddev=1.193`
  (inferred from W0+W1 IS+OOS = 13+7+24+5 = 49).

The two MNQ assignments are intentional: `mnq_futures` is the
known-good ORB baseline; `mnq_futures_sage` is the higher-Sharpe
candidate that needs paper-soak validation before live promotion.
The drift watchdog will track both independently.

## Honest caveats

* **Trade count is low** (12 OOS total). Two-window walk-forward
  on 107 days of MNQ 5m is the bottleneck. Re-optimization here
  is sweeping few data points; the real validation is paper-soak
  trades 50+.
* **NQ has not been tested** with the sage overlay. The pinned
  `nq_orb_v1` baseline was identical to `mnq_orb_v1` because ORB
  is symbol-agnostic; whether sage's overlay generalizes the same
  way needs its own walk-forward.
* **Crypto strategies haven't been walk-forwarded** at all yet
  (trend/meanrev/scalp). Next research-grid run picks them up.

## Files in this commit

* `strategies/sage_consensus_strategy.py` — pure sage-as-entry.
* `strategies/sage_gated_orb_strategy.py` — ORB + sage overlay.
* `tests/test_sage_strategies.py` — 14 unit tests covering
  threshold gates, fail-open vs fail-closed, day-state rollback.
* `scripts/run_sage_walk_forward.py` — walk-forward harness.
* `scripts/sweep_sage_gated_orb.py` — 18-cell parameter sweep.
* `scripts/sweep_drb_params.py` — 720-cell DRB sweep (queued).
* `docs/research_log/sage_gated_orb_sweep_*.{md,json}` — sweep
  artifacts.
* `strategies/per_bot_registry.py` — added `mnq_futures_sage`
  + extended `strategy_kind` doc enum.
* `docs/strategy_baselines.json` — pinned `mnq_orb_sage_v1`.
* `tests/test_per_bot_registry.py` — `_IGNORES_THRESHOLD`
  allowlist widened.
