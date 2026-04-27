# eta-walkforward

> **STATUS: ready for release, not yet published.** This package is
> packaging-complete and tested but has not been pushed to PyPI or
> a public GitHub repo. The decision to open-source is reserved for
> the operator. When the time comes, the only steps are:
> (1) push `packages/eta-walkforward/` to a public repo,
> (2) `python -m build && twine upload` to PyPI.
> Until then the package lives inside the private monorepo and is
> consumable via `pip install -e packages/eta-walkforward`.

Honest walk-forward strategy evaluation. The boring, load-bearing parts
of the [Evolutionary Trading Algo](https://evolutionarytradingalgo.com/)
framework, extracted as a standalone Python package so anyone can use
the same gate, statistical guards, and drift watchdog the production
fleet runs against.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

## What this is

A small, opinionated library for the parts of strategy evaluation
that should not be opinions:

- **A strict walk-forward gate** that requires positive aggregate
  in-sample Sharpe (catches the lucky-OOS-split trap), bounded
  IS→OOS degradation, and per-fold consistency. Three modes:
  - `strict`: per-fold Deflated Sharpe Ratio pass-fraction
    (default for intraday strategies that fire many trades per fold).
  - `long_haul`: positive-fold-fraction (for daily/weekly cadence
    bots where 1-3-trade folds can't produce stable per-fold DSR).
  - `grid`: profit-factor + bounded drawdown (for market-making /
    grid trading strategies where Sharpe is the wrong metric).
- **Sharpe ratio with FP-noise guards.** The standard `if sd == 0`
  check misses two real degenerate patterns:
  1. Float-rounding noise (sd ≈ 1e-17 from constant-returns) can
     produce Sharpe = -1.2e+16.
  2. Deterministic R-multiple wins (every winner hits the same
     `+rr_target`) produce sd/mean ratios at ~1e-5 — also meaningless.
  Both caught by a relative-dispersion guard at 1e-3.
- **Deflated Sharpe Ratio** computation accounting for skew,
  kurtosis, and the number of parameter trials (Bailey & López de
  Prado 2014).
- **Drift monitor** comparing live-fill statistics against a frozen
  promotion-time `BaselineSnapshot` via z-score on win-rate and
  average-R. Severity tiers: green / amber / red.

## Why publish it

Every promoted strategy in the parent project has cleared exactly
this gate. Open-sourcing the gate makes the methodology auditable —
beta testers, regulators, and other quants can run the same checks
on their own strategies and reproduce or falsify results.

It's also the answer to "what's so special about your framework?":
*nothing about this package is.* The edge is in the strategies and
the data; the gate is just rigor. We give the rigor away.

## Install

```bash
pip install eta-walkforward
```

Or from source:

```bash
git clone https://github.com/edwardtavila-boop/eta-walkforward
cd eta-walkforward
pip install -e .[test]
pytest
```

Python 3.11+ required.

## Quickstart

### Score a strategy through the strict gate

```python
from eta_walkforward import (
    BacktestConfig,
    BacktestEngine,
    WalkForwardConfig,
    WalkForwardEngine,
)
from datetime import datetime, timezone

# Your strategy is any object with this method:
#   maybe_enter(bar, hist, equity, config) -> _Open | None
# See eta_walkforward.engine for the _Open dataclass.
class MyStrategy:
    def maybe_enter(self, bar, hist, equity, config):
        ...

bars = ...   # list[BarData] from your data source

base_cfg = BacktestConfig(
    start_date=bars[0].timestamp,
    end_date=bars[-1].timestamp,
    symbol="MNQ1",
    initial_equity=10_000.0,
    risk_per_trade_pct=0.01,
    confluence_threshold=0.0,
    max_trades_per_day=10,
)
wf_cfg = WalkForwardConfig(
    window_days=60, step_days=30,
    anchored=True, oos_fraction=0.3,
    min_trades_per_window=3,
    strict_fold_dsr_gate=True,
    fold_dsr_min_pass_fraction=0.5,
)
result = WalkForwardEngine().run(
    bars=bars,
    config=wf_cfg,
    base_backtest_config=base_cfg,
    strategy_factory=lambda: MyStrategy(),
)

print(f"agg IS Sharpe   = {result.aggregate_is_sharpe:+.3f}")
print(f"agg OOS Sharpe  = {result.aggregate_oos_sharpe:+.3f}")
print(f"DSR pass        = {result.fold_dsr_pass_fraction * 100:.1f}%")
print(f"Verdict         = {'PASS' if result.pass_gate else 'FAIL'}")
```

### Long-haul gate (daily-cadence bots)

```python
wf_cfg = WalkForwardConfig(
    window_days=365, step_days=180,
    long_haul_mode=True,
    long_haul_min_pos_fraction=0.55,
    min_trades_per_window=3,
)
```

### Grid-mode gate (market-making strategies)

```python
wf_cfg = WalkForwardConfig(
    window_days=90, step_days=30,
    grid_mode=True,
    grid_min_profit_factor=1.3,
    grid_max_dd_pct=20.0,
)
```

### Drift monitoring against a frozen baseline

```python
from eta_walkforward import BaselineSnapshot, assess_drift

baseline = BaselineSnapshot(
    strategy_id="my_strategy_v1",
    n_trades=120,
    win_rate=0.55,
    avg_r=0.42,
    r_stddev=1.2,
)

assessment = assess_drift(
    strategy_id="my_strategy_v1",
    recent=live_trades_last_30_days,  # Sequence[Trade]
    baseline=baseline,
    min_trades=20,
    amber_z=2.0,
    red_z=3.0,
)

print(assessment.severity)   # 'green' | 'amber' | 'red'
print(assessment.reasons)    # human-readable list
```

## What the strict gate actually requires

For `pass_gate = True`:

1. **Aggregate IS Sharpe > 0** — the strategy works on training data.
   Closes the lucky-OOS-split trap where IS-negative strategies
   pass via `_degradation` returning 0 when IS ≤ 0.
2. **Aggregate OOS Sharpe > 0** — implicit via the DSR check below.
3. **Aggregate degradation < 35%** — bounded IS→OOS gap. Per-window
   degradation is clamped to [0, 1] so a single small-IS window
   can't blow up the average.
4. **Aggregate Deflated Sharpe Ratio > 0.5** — adjusts for skew,
   kurtosis, and the number of parameter trials.
5. **`min_trades_met_fraction` ≥ 80%** of windows hit their trade
   count. Selective strategies (2-8 trades per OOS window) aren't
   structurally locked out by this.
6. **In strict mode**: per-fold DSR median > 0.5 AND per-fold DSR
   pass-fraction ≥ 50%.
7. **In long-haul mode**: positive-fold-fraction ≥ 55% (replaces
   per-fold DSR).
8. **In grid mode**: profit-factor ≥ 1.3 AND worst-fold drawdown ≤
   20% AND positive-fold-fraction ≥ 55% (replaces DSR + degradation).

## What the FP-noise guard catches

Two real bugs that hit the parent project:

```python
# Pattern 1: floating-point rounding noise
returns = [-0.01, -0.01, -0.010000000000000023]
# sd = 1.3e-17 (mathematically should be 0)
# Old: Sharpe = mean / sd ≈ -1.2e+16
# New: returns 0.0 (sd / abs(mean) < 1e-3)

# Pattern 2: deterministic R-multiple wins
returns = [0.015, 0.015, 0.014999636, 0.014999837]
# sd = 1.7e-7, mean = 0.015, ratio = 1.15e-5
# Old: Sharpe = 1.4e+6
# New: returns 0.0 (cross-trade dispersion below meaningful threshold)
```

Real strategy returns have ≥0.1% relative cross-trade dispersion
from slippage, partial fills, and target-distance variance.
Anything tighter is degenerate by construction.

## Why use this over a one-line Sharpe ratio

You don't need this if you're running a single 5-year backtest
with hundreds of trades, no parameter search, and you trust your
own statistics chops. You probably want it if:

- You're running a parameter sweep across many configurations and
  worry about selection bias (the DSR formula adjusts for it).
- You're evaluating selective strategies that fire 2-8 trades per
  out-of-sample window (the long-haul gate handles this).
- You're shipping live capital and want a drift watchdog that compares
  current trades against a frozen baseline (the drift_monitor module).
- You want gate semantics that survive code review by an independent
  quant — the gate is documented, tested, and stable.

## Stability commitments

- All public-facing types (`BacktestConfig`, `WalkForwardConfig`,
  `WalkForwardResult`, `BaselineSnapshot`, `DriftAssessment`) are
  stable for 0.x releases. New fields are added with sensible defaults.
- Gate semantics are versioned. If we tighten the strict gate in a
  future release, we add a new `gate_version` field and keep the
  previous behavior reachable for backward compat.
- Bug fixes in `compute_sharpe` (FP-noise guards, IS-positive check)
  are NOT semantic-breaking. We consider them numerical correctness.

## Where this package came from

Extracted from the production codebase of
[Evolutionary Trading Algo LLC](https://evolutionarytradingalgo.com/)
on 2026-04-27. The gate, FP-noise guards, and drift monitor each
landed in response to a specific real-world failure documented in
the project's [public research log](https://evolutionarytradingalgo.com/research/).

If you find the gate too strict (you shouldn't be able to pass it
on overfit strategies — that's the point), file an issue with the
walk-forward output and we'll discuss it openly.

## License

MIT — see [LICENSE](LICENSE). Use freely. Attribution appreciated
but not required.
