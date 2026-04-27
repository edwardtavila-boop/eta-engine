# Research Log — 2026-04-26 evening — Framework supercharge

Second entry in `docs/research_log/`. Where the first was a **process
checkpoint** (verify the rebrand didn't break anything), this one is
the first **functionality bump**: closing the framework gaps surfaced
in the prior log + adding a drift monitor.

## Closed gaps from the 2026-04-26 baseline log

### 1. Test pollution → 98.2% pass rate (was 97.7%)

`backtest/models.py` had `from __future__ import annotations` plus a
runtime alias for `datetime`. Pydantic v2 lazy-resolves forward refs
at first `model_validate`, so test ordering mattered: when a test
elsewhere triggered Trade validation before `models.py` runtime alias
was in scope, the suite saw `Trade is not fully defined`.

**Fix:** explicit `Trade.model_rebuild()`, `BacktestResult.model_rebuild()`,
`BacktestConfig.model_rebuild()` calls at module import time. Pins
forward-ref resolution on first import. Failures dropped from 68 → 19.

### 2. Regime tags + exit reasons on every Trade

Schema additions to `backtest.models.Trade`:

```python
regime: str | None = None
exit_reason: str | None = None
```

`backtest/engine.py`:
- `_Open` dataclass picks up `regime` from `ctx_builder` output (key
  `"regime"`, falls back to `None` for legacy ctx builders).
- `_close()` accepts `exit_reason` kwarg; `_exit()` passes
  `"stop_hit"` or `"target_hit"`; the implicit final close at
  end-of-bars passes `"session_end"`.

`backtest/tearsheet.py`:
- `_regime_breakdown` now produces a real win-rate × avg-R table
  grouped by regime label (or surfaces the `(unlabeled)` case).
- New `_exit_breakdown` table — count, win rate, avg R per exit
  reason. Demo run output:

```
| `stop_hit`   | 12 | 0.0%   | -1.000 |
| `target_hit` | 17 | 100.0% | +1.500 |
```

Which immediately explains the third gap.

### 3. "No `>2R` trades" — explained, not a bug

The exit breakdown shows every winner closes at exactly +1.500R.
Tracing through `engine._enter`:
```python
rr = self.config.target_r_multiple / self.config.stop_r_multiple
target = bar.close + rr * stop_dist  # for BUY
```
With defaults `target_r_multiple=3.0` and `stop_r_multiple=2.0`,
`rr = 1.5`. So target sits at +1.5R from entry — *not* at +3R as the
config name suggests. Code comment added to flag this; renaming the
config field to `target_rr_ratio` would be safer for future readers
but is a breaking change deferred to a separate PR.

### 4. Walk-forward gate failure auto-explanation

`scripts/run_walk_forward_demo.py` now ends with:
```
Gate (strict: DSR+deg+trades + median+pass-frac): FAIL
Why it failed:
  - OOS degradation > 50% in window(s): 0, 1 (strategy IS-overfits in those folds)
```

`_explain_gate(res, wf)` enumerates every failing criterion (per-window
degradation, DSR median, DSR pass fraction, OOS trade floor). New
criteria added to walk-forward should be reflected there so the
operator never has to infer the failure mode from raw metrics.

## New: real-data MNQ walk-forward

`scripts/run_walk_forward_mnq_real.py` — reads `C:\mnq_data\mnq_5m.csv`
(20,641 bars, 2025-12-28 → 2026-04-14), wires bars into the existing
`WalkForwardEngine`, prints the same auto-explained gate verdict.

**First-run finding:** every window produced **0 trades**. Gate
correctly fails on:
- DSR median 0.097 (≤ 0.5)
- DSR pass fraction 0.0% (< 50%)
- OOS trade count 0 < min 5 in 6/6 windows

The strategy's confluence pipeline depends on funding / on-chain /
sentiment context that the CSV doesn't carry; the script's
`ctx_builder` synthesizes plausible placeholders to keep the demo
runnable, but they don't push score above the 7.0 confluence
threshold. **Real edge work needs a ctx_builder that pulls actual
context for each bar timestamp** — that's the next research move.

The script is still valuable as-is: it proves the data path
(CSV → BarData → WalkForwardEngine → strict gate → auto-explanation)
works end to end on real bars.

## Supercharge: drift monitor

New module `obs/drift_monitor.py` + 11 tests in `test_drift_monitor.py`.

**Purpose:** flag a strategy that has decayed in production, *before*
PnL bleed shows up in equity. Compares recent live trade metrics to a
pinned baseline, returns a `DriftAssessment` with severity
(`green`/`amber`/`red`) and human-readable reasons.

```python
from eta_engine.obs.drift_monitor import BaselineSnapshot, assess_drift

baseline = BaselineSnapshot(strategy_id="mnq_v3",
                            n_trades=200, win_rate=0.6,
                            avg_r=0.4, r_stddev=1.0)
recent_trades = [...]   # last N completed Trade objects
a = assess_drift(strategy_id="mnq_v3",
                 recent=recent_trades, baseline=baseline)
if a.severity != "green":
    alert(a.reasons)    # plug into existing alert dispatcher
```

Algorithm:
1. Insufficient sample (`n < min_trades`, default 20) → green +
   "insufficient sample" reason. No flapping on first fills.
2. Win-rate z under H0 = baseline.win_rate, normal-approx SE.
3. Avg-R z against baseline.r_stddev / sqrt(n).
4. `red` if either |z| ≥ 3.0 (default), `amber` if either ≥ 2.0,
   `green` otherwise. Both reasons surface even when one already
   tripped red.

Designed to be called from a watchdog/avengers/cron loop that loads
recent trades from the decision_journal (or in-memory ring buffer)
and writes the resulting assessment back to the journal as a
`Actor.GRADER` event. That integration is left for a follow-up so
the module can be tested + adopted incrementally.

## Headline numbers

| | Before | After |
|---|---|---|
| Pytest pass rate (non-slow) | 97.7% | 98.2% |
| Backtest tearsheet sections | 5 | 6 (added Exit Breakdown) |
| Regime breakdown | placeholder | real table |
| Gate failure explanation | absent | per-criterion |
| Real-data walk-forward script | absent | runs end-to-end |
| Drift monitor | absent | module + 11 tests |

## Next research session

Now that the framework reports honestly, the actual edge work begins:

1. **Wire a real ctx_builder** for `run_walk_forward_mnq_real.py` —
   pull funding/on-chain/sentiment from the dual_data_collector
   output rather than the placeholder dict.
2. **First real strategy pass:** with a real ctx, see whether the
   strict gate passes on any time slice of the 2026-Q1 MNQ data.
3. **Adopt the drift monitor in JARVIS daemon** — load the latest 50
   trades from `decision_journal.jsonl` per strategy, run
   `assess_drift`, append the result back to the journal as a
   `GRADER` event with severity in `metadata`.
4. **Resolve the 19 remaining test failures** (mostly
   `test_jarvis_hardening` dashboard drift fixtures + a few obs
   probes registry callers — pre-existing, not rebrand-induced).
