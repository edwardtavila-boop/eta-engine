# Strategy Supercharge Manifest, 2026-04-30

This batch turns the scorecard queue into framework-readable work orders for
the approved `A+C then B` sequence.

Important scope correction: this is a cross-asset, multi-style strategy
supercharge surface, not an MNQ-only effort. The full manifest currently covers
`BTC`, `ETH`, `SOL`, `MNQ1`, and `NQ1` across ensemble voting, sage
daily/gated, compression breakout, crypto ORB, regime/macro confluence, ORB,
DRB, and legacy confluence rows. The current A+C smoke results cover the A+C
subset (`BTC`, `ETH`, `SOL`, `MNQ1`); NQ remains visible in the B-later
live-preflight bucket.

## What landed

The manifest adds one safe action row per scorecard bot:

1. A+C paper/research/shadow rows emit runtime-only `run_research_grid`
   retest commands.
2. A+C data-repair rows emit `bot_strategy_readiness` recheck commands.
3. B live-preflight rows emit `preflight_bot_promotion` commands, but remain
   deferred until A+C is stable.
4. Hold rows emit no command.
5. A+C research rows also emit `smoke_command` variants with timeframe-aware
   `--max-bars-per-cell` caps so smoke retests produce real walk-forward
   windows instead of zero-window false noise.

Every row keeps `safe_to_mutate_live=false` and `writes_live_routing=false`.

## Runtime surfaces

```powershell
python -m eta_engine.scripts.strategy_supercharge_manifest
curl http://127.0.0.1:8000/api/jarvis/strategy_supercharge_manifest
```

The CLI writes:

`C:\EvolutionaryTradingAlgo\var\eta_engine\state\strategy_supercharge_manifest_latest.json`

The manifest exposes `scope` and `groups` so framework clients can group the
queue by ticker and strategy style without guessing from old repo names.

The dashboard now also embeds the manifest at:

```text
/api/dashboard.strategy_supercharge_manifest
```

and registers the Command Center card:

```text
cc-strategy-supercharge -> /api/jarvis/strategy_supercharge_manifest
```

## First smoke retest evidence

The current A+C retest evidence is collected by:

```powershell
python -m eta_engine.scripts.strategy_supercharge_results
curl http://127.0.0.1:8000/api/jarvis/strategy_supercharge_results
```

The collector reads the canonical manifest snapshot first, then parses
runtime-only research-grid markdown reports newer than that manifest. Older
reports are kept as `stale_report_path` references but do not count as current
evidence.

The results payload also exposes `scope`, `groups.by_symbol`,
`groups.by_strategy_kind`, per-row `retune_plan`, and a ranked
`retune_queue`. The current A+C result groups are:

| Symbol | Targets | Passed | Failed | Best near-miss |
| --- | ---: | ---: | ---: | --- |
| `BTC` | 5 | 0 | 5 | `btc_hybrid_sage` |
| `ETH` | 3 | 1 | 2 | `eth_sage_daily` |
| `MNQ1` | 2 | 0 | 2 | none |
| `SOL` | 1 | 0 | 1 | `sol_perp` |

Current result summary after the full A+C smoke sweep:

| Metric | Count |
| --- | ---: |
| Tested | 11 |
| Passed | 1 |
| Failed | 10 |
| Pending | 0 |

Current retest rows:

| Bot | Windows | OOS Sharpe | DSR pass | Verdict |
| --- | ---: | ---: | ---: | --- |
| `btc_ensemble_2of3` | 21 | -0.221 | 23.8% | FAIL |
| `btc_sage_daily_etf` | 21 | +1.708 | 42.9% | FAIL |
| `eth_compression` | 21 | +0.053 | 28.6% | FAIL |
| `eth_perp` | 21 | +1.929 | 52.4% | PASS |
| `btc_hybrid_sage` | 21 | +8.662 | 47.6% | FAIL |
| `btc_regime_trend_etf` | 21 | +0.724 | 42.9% | FAIL |
| `mnq_sage_consensus` | 3 | +0.000 | 0.0% | FAIL |
| `btc_compression` | 21 | +0.704 | 38.1% | FAIL |
| `eth_sage_daily` | 21 | +4.888 | 57.1% | FAIL |
| `mnq_futures` | 3 | -1.355 | 0.0% | FAIL |
| `sol_perp` | 21 | +2.489 | 52.4% | FAIL |

Interpretation: the smoke path is now executable and produces meaningful
walk-forward evidence. `eth_perp` cleared the strict smoke gate and should
move to paper-soak/promotion review, while the other current A+C bots stay in
retest/soak work, not live-preflight promotion.

The strongest near-miss from the results collector is `eth_sage_daily`,
followed by `sol_perp`, `btc_hybrid_sage`, `btc_sage_daily_etf`, and
`btc_regime_trend_etf`.

Current ranked retune queue:

| Rank | Bot | Issue | Primary knobs |
| ---: | --- | --- | --- |
| 1 | `eth_sage_daily` | `strict_gate_near_miss` | `min_daily_conviction`, `strict_mode`, `vol_band_lookback`, `min_macro_score` |
| 2 | `sol_perp` | `strict_gate_near_miss` | `range_minutes`, `atr_stop_mult`, `rr_target` |
| 3 | `btc_hybrid_sage` | `positive_oos_unstable` | `range_minutes`, `atr_stop_mult`, `rr_target`, `min_conviction`, `min_alignment` |
| 4 | `btc_sage_daily_etf` | `positive_oos_unstable` | `min_daily_conviction`, `strict_mode`, `vol_band_lookback`, `min_macro_score` |
| 5 | `btc_regime_trend_etf` | `positive_oos_unstable` | `vol_band_lookback`, `min_macro_score`, `require_eth_alignment`, `extreme_funding_threshold` |

## First scoped retune

Before the refreshed full-window smoke, `sol_perp` was the strongest near-miss,
so we ran:

```powershell
python -m eta_engine.scripts.fleet_strategy_optimizer --only-bot sol_perp --out-dir C:\EvolutionaryTradingAlgo\var\eta_engine\state\strategy_supercharge_retunes
```

Result:

| Bot | Cells | PASS configs | Optimizer closest fail | Notes |
| --- | ---: | ---: | --- | --- |
| `sol_perp` | 31 | 0 | `crypto_orb: corb r240/atr2.0/rr2.0` | No strict PASS; registered `sol_corb_v2` kept strong OOS but negative IS, so it stays a near-miss rather than a promotion. |

Runtime report:

`C:\EvolutionaryTradingAlgo\var\eta_engine\state\strategy_supercharge_retunes\fleet_optimization_20260430T035838Z.md`

After the refreshed full-window smoke, `eth_sage_daily` is now the first
retune-queue item and `sol_perp` remains second. Both are still advisory
retune candidates only; no registry or live-routing mutation is implied.

## Why this matters

The scorecard tells us which bots deserve attention. The manifest tells JARVIS,
the dashboard, and wakeup automation exactly which command to run first without
requiring shell scraping or manual copy/paste. It also makes the B boundary
explicit: live-preflight bots are visible, but deferred until A+C retests prove
stable.

## Files touched

- `scripts/strategy_supercharge_manifest.py`
- `scripts/strategy_supercharge_results.py`
- `deploy/scripts/dashboard_api.py`
- `tests/test_strategy_supercharge_manifest.py`
- `tests/test_strategy_supercharge_results.py`
- `tests/test_dashboard_api.py`
- `docs/live_launch_runbook.md`
- `docs/research_log/strategy_supercharge_manifest_20260430.md`
- `roadmap_state.json`
