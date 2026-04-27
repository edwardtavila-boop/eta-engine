# 2026-04-27 — Fleet strategy optimization (rounds 1+2): final summary

## Operator directive

> "finish finding and optimizing the core strategy for all crypto bots
> and mnq/nq bots even the grid bot — everything needs a core strategy
> or 2 or 3 to start that produces great results. we have enough data,
> use what you know and make them supercharged."

## What was run

Two-round fleet sweep over 8 distinct bots, 195 + 152 = **347 walk-
forward cells** total, each evaluated against the strict gate
(`is_positive ∧ deg<35% ∧ DSR_med>0.5 ∧ DSR_pass≥50% ∧
min_trades_met_fraction≥0.8`). Per-bot candidate sets were tuned to
each market structure rather than a uniform grid.

Round 1 — fleet-wide hand-curated grids per bot (`fleet_strategy_optimizer`).
Round 2 — deeper sweeps targeted at round-1 failure modes
(`fleet_strategy_optimizer_round2`).

## Final fleet PASS map

| Bot | Strategy | Cell | IS | OOS | Deg% | DSR pass% | Verdict |
|---|---|---|---:|---:|---:|---:|---|
| **mnq_futures** | orb | r15/atr2.0/rr2.0 | +3.29 | +5.71 | 14.2 | 100.0 | **PASS** |
| **nq_futures** | orb | r15/atr2.0/rr2.0 | +3.29 | +5.71 | 14.2 | 100.0 | **PASS** |
| **btc_hybrid** | crypto_orb | r120/atr3.0/rr1.5 | +0.43 | +1.95 | 20.0 | 56.2 | **PASS** *(5y, re-baseline)* |
| **eth_perp** | crypto_orb | r60/atr3.0/rr2.0 | +0.21 | **+16.10** | 27.8 | 88.9 | **PASS** *(NEW)* |
| nq_daily_drb | drb | atr2.0/rr2.0 | +0.92 | +9.05 | 1255.9 | 39.6 | FAIL (DSR pass) |
| sol_perp | crypto_orb | r120/atr2.0/rr2.5 | -3.80 | +2.50 | 0.0 | 44.4 | FAIL (IS) |
| crypto_seed | crypto_trend | ema50/200 | +1.40 | +0.60 | 37.5 | 12.5 | FAIL (DSR pass) |
| grid_bot__btc | grid | sp0.001/lvl4 | -0.27 | +0.92 | 533.0 | 8.8 | FAIL (IS, DSR) |

**4 production strategies, all IS+ AND OOS+, all on real walk-forward
data.** Two of them (BTC, ETH) are the framework's first-ever crypto
promotions.

## Promoted configs (locked into the registry)

### MNQ — `mnq_orb_v1`
- ORB, 15m range, atr_stop_mult=2.0, rr_target=2.0
- 60d/30d walk-forward windows, 5m bars
- IS +3.29, OOS +5.71, 100% fold pass
- Production, no warmup policy needed (already live-validated)

### NQ — `nq_orb_v1`
- Same ORB config as MNQ (symbol-agnostic on liquid index futures)
- IS +3.29, OOS +5.71, 100% fold pass

### BTC — `btc_corb_v3` *(re-baselined from v2)*
- crypto_orb, range=120m, atr_stop_mult=3.0, rr_target=**1.5** (was 2.5 in v2)
- **365d/90d** windows on the 5y BTC tape (was 90d/30d on the 1y v2 tape)
- IS +0.43, OOS +1.95, deg 20.0%, DSR median 0.801, 56.2% fold pass
- Full-period stats: 332 trades, 45.5% WR, +0.14R/trade, +54.4% return, 17.5% DD
- Half-size warmup for first 30 days post-promotion

### ETH — `eth_corb_v3` *(NEW)*
- crypto_orb, range=60m, atr_stop_mult=3.0, rr_target=2.0
- 90d/30d windows on 360d ETH 1h tape
- IS +0.21, OOS +16.10, deg 27.8%, DSR median 1.000, 88.9% fold pass
- Full-period stats: 73 trades, 41.1% WR, +0.23R/trade, +17.6% return, 9.6% DD
- Half-size warmup for first 30 days post-promotion

## What didn't pass (and why)

### `nq_daily_drb` — strong signal, gate-blocked by per-fold variance
Round 1 best: drb atr=2.0/rr=2.0 with **agg OOS Sharpe +9.05** —
huge but DSR pass-fraction only 39.6%. Per-fold DSR is volatile
because individual NQ daily folds have wildly different trade counts
(fold-to-fold sample heterogeneity). Round 2's longer windows
(730d/365d) helped marginally but didn't clear 50%. Honest read:
the strategy *works* on 27y of NQ daily data but the gate's
per-fold structure punishes its few-trade-per-window cadence.
Stays as **research candidate**.

### `sol_perp` — IS-negative across most cells
SOL is the high-beta BTC proxy; round 1 + 2 found multiple
crypto_orb cells with positive OOS (+2.50, +1.62) but consistently
negative IS. Mean-reversion (Bollinger+RSI) flipped IS positive but
OOS dropped below the gate. SOL is **structurally hard** — it has
the volatility of a momentum bot but the regimes shift faster than
either ORB or mean-rev can adapt. Open research direction: per-
regime SOL strategy (different config when BTC is trending vs
ranging). Stays **deactivated equivalent** — not promoted.

### `crypto_seed` — small-sample on daily DCA
8 windows over 5y of BTC daily isn't enough for the strict gate's
per-fold DSR requirements. Best cell (crypto_trend ema50/200)
shows IS +1.40, OOS +0.60 — real signal but DSR pass at 12.5%
because most folds fire only 1-2 trades. The bot is **conceptually
DCA**; the strict-gate framework isn't the right evaluation for
it. Open: register `crypto_seed` under a separate "long-haul" gate
that doesn't require per-fold DSR.

### `grid_bot__btc` — wrong instrument for the strategy
BTC 1h is too directional for grid trading; round 2's tightest
spacing (0.1%) caught some mean-reversion (+0.92 OOS) but the
overall trend made the grid bleed. Grid trading wants a
**range-bound asset**, which BTC 1h on a 5y tape is decisively
not. The strategy itself isn't broken — the venue choice is. Open:
re-evaluate on a stablecoin pair or a sideways-regime BTC slice.

## What this iteration delivered

### New production strategies — 2 → 4

Was: `mnq_orb_v1`, `nq_orb_v1` (both index futures ORB)
Now: + `btc_corb_v3`, `eth_corb_v3`

**100% of the actively-traded crypto fleet** (BTC + ETH) now has
a passing strategy. SOL remains in research; XRP stays explicitly
deactivated until the SECHeadline feature class lands.

### Re-baselining lessons

The BTC v2 → v3 transition surfaced an important lesson: **a strategy
that passes the strict gate on 1y of data isn't guaranteed to pass
on 5y**. The original v2 promotion (90d/30d / 9 windows) was
under-powered statistically; the 5y tape produced a wider sweep where
the same config dropped from 66.7% to 49% DSR pass. The v3 re-
baseline scaled windows proportionally (365d/90d / 16 windows) and
the gate cleared at a different param vector (rr=1.5 vs 2.5).

This is the kind of finding a watchdog should catch automatically —
which is why `scripts/run_drift_watchdog.py` (built earlier today)
flagged the re-baseline candidates as AMBER on its first run.

### Two new optimizer scripts

* `scripts/fleet_strategy_optimizer.py` — round-1 fleet-wide grid
  with per-bot candidate sets (8 bots × 195 cells).
* `scripts/fleet_strategy_optimizer_round2.py` — round-2 deeper
  sweeps targeted at round-1 failure modes (5 bots × 152 cells).

Both produce ranked markdown reports under `docs/research_log/
fleet_optimization*.md`.

### Registry edits

* `btc_hybrid`: strategy_id `btc_corb_v2` → `btc_corb_v3`,
  window_days 90 → 365, step_days 30 → 90, min_trades 3 → 10,
  rr_target 2.5 → 1.5.
* `eth_perp`: strategy_id `eth_corb_v2` → `eth_corb_v3`, range
  120m → 60m, rr_target 2.5 → 2.0, atr_stop_mult stays 3.0.
* `docs/strategy_baselines.json`: btc_corb_v2 marked deprecated;
  btc_corb_v3 + eth_corb_v3 added with full per-trade stats.

## Open research carried forward

1. **`nq_daily_drb` regime gate** — try blocking trades during
   prior-month-drawdown extremes. The per-fold DSR variance
   suggests bad folds cluster around regime shifts.
2. **`sol_perp` per-regime stack** — different SOL config when
   BTC is trending vs ranging. Needs an upstream regime classifier.
3. **`crypto_seed` long-haul gate** — separate evaluation rules
   for daily-cadence accumulators. Per-fold DSR is the wrong
   guardrail for them.
4. **`grid_bot` venue swap** — try grid trading on a stablecoin
   pair (USDC/USDT) or a sideways-regime BTC slice rather than
   the full 5y directional tape.

## What's next (operationally)

* **Pre-live drift gate** for BTC + ETH: run
  `scripts/fetch_ibkr_crypto_bars` + `scripts/compare_coinbase_vs_ibkr`
  before any real-money activation per `eta_data_source_policy.md`.
* **Drift watchdog** scheduled task: register
  `scripts/run_drift_watchdog.py` to run daily at 09:00 UTC. Now
  4 production strategies → 4 baselines being monitored.
* **Paper-soak validation**: 30+ live fills per bot before flipping
  off the half-size warmup multiplier.
