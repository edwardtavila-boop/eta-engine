# Strategy Supercharge Manifest, 2026-04-30

This batch turns the scorecard queue into framework-readable work orders for
the approved `A+C then B` sequence.

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

Current result summary after four smoke retests:

| Metric | Count |
| --- | ---: |
| Tested | 4 |
| Passed | 0 |
| Failed | 4 |
| Pending | 7 |

Current retest rows:

| Bot | Windows | OOS Sharpe | DSR pass | Verdict |
| --- | ---: | ---: | ---: | --- |
| `btc_ensemble_2of3` | 2 | +0.535 | 50.0% | FAIL |
| `btc_sage_daily_etf` | 2 | +0.392 | 50.0% | FAIL |
| `eth_compression` | 2 | +0.750 | 50.0% | FAIL |
| `eth_perp` | 2 | -2.291 | 0.0% | FAIL |

Interpretation: the smoke path is now executable and produces meaningful
walk-forward evidence, but no current A+C smoke slice has cleared the strict
gate yet. These bots stay in retest/soak work, not live-preflight promotion.

## Why this matters

The scorecard tells us which bots deserve attention. The manifest tells JARVIS,
the dashboard, and wakeup automation exactly which command to run first without
requiring shell scraping or manual copy/paste. It also makes the B boundary
explicit: live-preflight bots are visible, but deferred until A+C retests prove
stable.

## Files touched

- `scripts/strategy_supercharge_manifest.py`
- `scripts/strategy_supercharge_results.py`
- `scripts/workspace_roots.py`
- `deploy/scripts/dashboard_api.py`
- `tests/test_strategy_supercharge_manifest.py`
- `tests/test_strategy_supercharge_results.py`
- `tests/test_dashboard_api.py`
- `docs/live_launch_runbook.md`
- `docs/research_log/strategy_supercharge_manifest_20260430.md`
