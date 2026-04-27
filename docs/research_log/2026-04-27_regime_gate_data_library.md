# Research Log — 2026-04-27 — Regime gate + data library

Fifth entry. Closes 2 of the 5 next-session candidates from the
previous log + introduces the **data library**, a catalog
infrastructure JARVIS can introspect to know what's testable
without filesystem grep.

## Regime gate (closes #1, partially closes #2)

`backtest/engine.py::BacktestEngine` now accepts `block_regimes:
frozenset[str] | None = None`. When set, `_enter()` short-circuits
to None whenever `ctx["regime"]` is in the blocked set — runs
*before* scoring so a blocked regime doesn't consume the
trades-per-day budget.

`backtest/walk_forward.py::WalkForwardEngine.run` threads the same
parameter into both IS and OOS BacktestEngine instances.

`scripts/run_walk_forward_mnq_real.py` blocks
`{trending_up, trending_down}` by default; override via
`MNQ_BLOCK_REGIMES="" python -m ...` for an ungated control run.

### Multi-config comparison

| Config | Windows | DSR pass % | Per-window edge |
|---|---:|---:|---|
| 5m, 6 windows, ungated | 6 | **0.0%** | W0 +1.27 only |
| 5m, 6 windows, gated   | 6 | **16.7%** | W0 +1.08, **W3 +1.40** |
| 1h, 96 windows, gated  | 96 | **22.9%** | mixed |
| 1h, 46 windows (90/30), gated | 46 | **28.3%** | 22 of 46 OOS-positive |

The gate moves DSR pass from 0% → 16–28% across configs. **Window 3
specifically jumped from neutral (+0.15 OOS Sharpe) to clearly
positive (+1.40)** — the gate revealed an edge that the ungated
strategy was masking with bad trending-regime entries.

Aggregate OOS Sharpe stays negative across all configs, because
the surviving losing windows (e.g. W4 at -7.80 OOS, W90 at -33.05)
still dwarf the winners. The gate makes the strategy *more
discriminating*; it does not make a fundamentally bad strategy good.

### Honest read

The strategy as configured is still NOT edge-positive on real MNQ
across the available history. But two findings push us forward:

1. The regime gate is structurally correct — pass rate moves the
   right direction, Window 3 went from indistinguishable to
   clearly positive when trending bars are filtered out.
2. The strict gate continues to refuse promotion despite multi-
   year data + 22-46% DSR pass — exactly what we want pre-CTA.

## Data library (closes #2, sets up future research)

New module: `data/library.py` + 30/30 tests in
`tests/test_data_library.py`.

`DataLibrary` walks `C:\mnq_data\` and `C:\mnq_data\history\`,
parses two filename schemes (`mnq_<asset>_<digits>.csv` "main"
shape, `<SYMBOL>_<TF>.csv` "history" shape), probes each file for
row count + first/last timestamps without loading the whole CSV,
and exposes:

* `list(symbol=..., timeframe=..., schema_kind=...)` — filter
* `get(symbol, timeframe)` — singular lookup, prefers longest-history
* `load_bars(dataset, limit=...)` — lazy bar load
* `summary_markdown()` — operator-readable table
* `summary_jarvis_payload()` — list-of-dicts for journaling

### What's in the catalog (live snapshot)

**33 datasets across 8 symbols and 8 timeframes:**

| Symbol | Best history | Note |
|---|---|---|
| **NQ1** | **D bars 1999-06-29 → 2026-04-13 (27 yrs, 6,775 rows)** | Deepest |
| **NQ1** | 4h 2013-01-02 → 2026-04-14 (13 yrs, 20,442 rows) | |
| **NQ1** | 1h 2022-01-02 → 2026-04-14 (4.3 yrs, 25,255 rows) | |
| **MNQ1** | **D bars 2019-05-05 → 2026-04-13 (7 yrs, 1,748 rows)** | |
| **MNQ1** | 4h 2019-05-05 → 2026-04-14 (7 yrs, 10,698 rows) | |
| **MNQ1** | 1h 2022-04-14 → 2026-04-14 (4 yrs, 23,572 rows) | |
| **MNQ1** | 15m 2025-06-01 → 2026-04-14 (10.5 mo, 20,464 rows) | |
| **MNQ1** | 5m 2025-12-28 → 2026-04-14 (3.5 mo, 20,722 rows) | |
| ES1, DXY, VIX, RTY1 | 1m/5m, 1–34 days | Cross-asset / macro |
| TICK | 1m/5m, 6–34 days | Order-flow context |

That's **27 years** of NQ daily, **7 years** of MNQ4h, plus
correlated tickers. Plenty of headroom for cross-asset features
and multi-decade walk-forward with the right pipeline.

### JARVIS adoption

`scripts/announce_data_library.py` emits the full inventory as a
single `Actor.JARVIS` event with `intent="data_inventory"` to the
decision journal. Re-run after any data fetch — the latest event
is the canonical "what's available" snapshot.

`run_walk_forward_mnq_real.py` now picks data via
`MNQ_SYMBOL` + `MNQ_TIMEFRAME` env vars (defaulting to MNQ1/5m).
Library returns the longest-history match automatically:

```bash
MNQ_SYMBOL=MNQ1 MNQ_TIMEFRAME=1h python -m eta_engine.scripts.run_walk_forward_mnq_real
```

Picks `MNQ1/1h/history` = 4-year dataset.

### Tests

30/30 in `test_data_library.py`:
- 13 filename-parse parametrised cases (all known shapes + 3 negatives)
- Discovery / filtering / get-prefers-history-when-ambiguous
- Schema-detection round-trip (main vs history)
- Bar loading with limit
- Markdown + JARVIS payload generation
- Robustness (missing root, empty CSV)

Pass rate stays at 99.83% on `pytest -m "not slow"`.

## What's left from the prior log

| # | Item | Status |
|---|---|---|
| 1 | Build regime-gated MNQ strategy | ✓ done |
| 2 | Confirm regime finding on more data | ✓ done (1h, 4h, 96-window run) |
| 3 | First baselined strategy | not yet — strategy isn't promotable |
| 4 | Test pollution bisect | still deferred |
| 5 | JARVIS daemon adoption | still deferred (standalone scheduled task is enough) |

## Next research session candidates

1. **Multi-decade walk-forward on NQ1 daily** — 27 years of daily
   bars + the regime gate. The strategy might fit very differently
   on daily than on 5m / 1h.
2. **Cross-asset features** — wire ES1, DXY, VIX into the
   ctx_builder. The data library makes them findable; a feature
   like "VIX z-score above 2σ" could be a useful regime signal.
3. **Why does W4 blow up?** — 1804% degradation on the 5m run is
   suspicious. Time-of-day clustering of losers? News event
   coincidence? Worth a single-window investigation à la the
   Window 0 deep-dive script.
4. **Replace ctx_builder with regime-aware version** — the bot
   uses the same regime tag I'm using to gate; pulling that into
   strategy-side ctx (rather than just the runner) makes the gate
   decision centralized and testable.
5. **Write the operator runbook** — `docs/operations/data_library.md`
   covering: how to refresh the catalog after a fetch, when to
   announce, how to query from a research script.

## Headline numbers

| | Before | After |
|---|---|---|
| Regime gate | absent | wired through engine + walk-forward |
| Available data per script | 1 hardcoded path | 33 catalogued datasets |
| Deepest tested timeframe | 5m, 3.5 months | 1h, 4 years |
| DSR pass on best config | 0% | 28.3% (90d windows, hourly, gated) |
| JARVIS-known data inventory | nope | event in journal |
| Pytest pass rate | 99.83% | **99.83%** (30 new tests, all pass) |
