# Strategy registry factory alignment, 2026-04-29

User directive:
> "improve all the bots strategies and fine tune them to become overall better"

This batch tightens the *strategy-resolution layer* so the fleet
actually runs the tuned configs already documented in the registry,
instead of silently falling back to generic defaults.

## What was wrong

Several promoted or research-tracked bots stored their winning knobs
in `per_bot_registry.py`, but the shared factory path did not fully
honor them:

- `orb_sage_gated` variants carried legacy `sage_*` and `orb_*` keys,
  but the builder used default `SageGatedORBConfig()`.
- `sage_daily_gated` variants could only build the hard-coded
  `crypto_macro_confluence` baseline, even when the registry pinned a
  different underlying strategy such as `crypto_orb`.
- `crypto_regime_trend` and `sage_consensus` rows often used direct
  registry fields rather than prefixed config blobs; those direct
  fields were being ignored in some paths.
- `run_drift_watchdog.py` special-cased ORB / DRB / ORB-sage variants
  with raw default configs, so recent-drift checks could disagree with
  the registry's intended live/backtest shape.

## What now resolves correctly

- `mnq_futures_sage` / `nq_futures_sage`
  - honor `orb_range_minutes`, `sage_min_conviction`,
    `sage_min_alignment`, and `instrument_class`
- `btc_hybrid_sage`
  - resolves as a **crypto** ORB + sage overlay instead of a futures
    ORB with defaults
- `mnq_sage_consensus`
  - honors its stricter `sage_*` thresholds instead of default
    consensus settings
- `btc_regime_trend`
  - honors its pinned 100/21/3.0/2.0/3.0 regime-trend profile
- `eth_sage_daily`
  - can wrap `crypto_orb` via the generic daily-sage gate instead of
    being forced through the fixed macro-confluence path
- `btc_sage_daily_etf` / `btc_regime_trend_etf` / `btc_ensemble_2of3`
  - can now declare the tuned 100/21 BTC regime-trend base, ETF-flow
    filter, and daily-sage gate knobs explicitly in the registry

## Why this matters

This is a foundation fix, not cosmetic cleanup. If the registry says a
bot's promoted edge depends on a specific ORB range, conviction floor,
or underlying strategy family, the builders must reproduce that exact
shape in:

- research-grid walk-forward runs
- drift-watchdog recent-window checks
- any other tooling that rehydrates strategy instances from the
  registry

Otherwise the framework is comparing the wrong strategy to the wrong
baseline.

## Files touched in the alignment batch

- `scripts/run_research_grid.py`
- `scripts/run_drift_watchdog.py`
- `strategies/per_bot_registry.py`
- `tests/test_registry_strategy_builders.py`

## Verification target

Focused unit coverage now checks that the builder layer preserves:

- crypto-vs-futures ORB selection for sage overlays
- legacy unprefixed regime-trend fields
- generic sage-daily gating over `crypto_orb`
- explicit macro-confluence base/filter configs for BTC daily-sage
- ensemble-voter reuse of the same BTC champion config stack
