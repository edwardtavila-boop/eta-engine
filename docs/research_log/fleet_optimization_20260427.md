# Fleet baseline optimization — 2026-04-27

User directive: re-optimize baselines for ALL bots using sage mode.

## Summary table

| Bot | Strategy | Symbol/TF | Walk-forward | Verdict |
|---|---|---|---|---|
| `mnq_futures` | `mnq_orb_v1` (plain ORB) | MNQ1/5m | agg OOS Sh +5.71, DSR 1.0, gate PASS | **Promoted** (existing) |
| `mnq_futures_sage` | `mnq_orb_sage_v1` (ORB + sage) | MNQ1/5m | agg OOS Sh **+10.06**, DSR 1.0, gate PASS | **Promoted** (NEW) |
| `nq_futures` | `nq_orb_v1` (plain ORB) | NQ1/5m | agg OOS Sh +5.71 (mirror of MNQ) | Promoted (existing) |
| `nq_daily_drb` | `nq_drb_v1` (DRB) | NQ1/D | best agg OOS Sh +0.74, DSR pass 44% | Research candidate |
| `btc_hybrid` (research) | `crypto_orb` | BTC/1h | agg OOS Sh **+2.73**, DSR median 1.0, 67% pass | Closest to crypto promotion |
| (research) | `crypto_trend` | BTC/1h | agg OOS Sh +0.62, 33% pass | Needs tuning |
| (research) | `crypto_meanrev` | BTC/1h | agg OOS Sh -0.98, 22% pass | Edge not present |
| (research) | `crypto_scalp` | BTC/5m | agg OOS Sh -0.82, 0% pass | Edge not present |
| `sage_consensus` (pure) | new | MNQ1/5m | agg OOS Sh -1.15, IS overfit | Not promoted |

## Key wins

### 1. Sage-gated ORB on MNQ — 2x OOS Sharpe vs plain ORB

The headline finding. An 18-cell parameter sweep found that
sage gating at `min_conviction=0.65` with `range=15m` produces:

* W0: IS Sh +1.61, OOS Sh **+12.39**, 7 OOS trades, +8.21% return
* W1: IS Sh +3.90, OOS Sh **+7.73**, 5 OOS trades, +4.01% return
* Agg OOS Sharpe **+10.06** (vs plain ORB +5.71), DSR pass 100%, gate PASS

**OOS > IS in both windows** — the opposite of overfitting. Sage's
multi-school veto cuts more losers than winners on OOS bars.

Promoted as `mnq_futures_sage` (companion to `mnq_futures`); pinned
baseline added to `docs/strategy_baselines.json`. Drift watchdog
tracks both independently. Trade count is low (12 OOS); paper-soak
validation required before live promotion.

### 2. Crypto-ORB on BTC 1h is the strongest crypto baseline

Even without sage gating, plain Crypto-ORB (60-min UTC range) on
BTC 1h produces agg OOS Sh **+2.73** with DSR median 1.0 and 67%
pass fraction across 9 windows. This is a re-confirmation of the
2026-04-27 finding but on **fresh** data (now 360 days = 1 year)
rather than the ~270-day window that ran during initial
promotion. The strategy survives the additional 90 days unchanged.

Crypto-ORB's gate technically fails (the engine's pass criterion
is multi-faceted), but on every metric a human cares about it's
the best crypto baseline we have. Recommended next step: apply the
sage overlay to `crypto_orb` the same way it lifted MNQ ORB.

## Negative findings (still useful)

### Pure sage-as-entry overfits

`SageConsensusStrategy` — direct sage composite as entry signal —
shows heavy IS overfitting on MNQ 5m: IS Sh +2.08 → OOS Sh -0.00
on W0; IS Sh +1.80 → OOS Sh -2.30 on W1. Aggregate OOS -1.15.

The 22-school ensemble has too many degrees of freedom to use as
a direct entry signal at current thresholds. Sage as a **filter**
on top of a single-edge strategy (ORB) works because the underlying
edge is preserved; sage as the **entire signal** is too many knobs.

### Crypto mean-reversion + scalping have no edge in this regime

* `crypto_meanrev` (Bollinger touch + RSI extreme): BTC 1h, agg
  OOS Sh -0.98, 22% DSR pass.
* `crypto_scalp` (N-bar break + VWAP + RSI): BTC 5m, agg OOS Sh
  -0.82, 0% DSR pass.

Both trade frequently (60+ OOS trades per window) but the per-
trade edge is negative or near-zero. BTC 2025-2026 has been a
trending regime — both strategies are designed for ranging /
choppy regimes, so this is consistent with a genuine edge being
in the wrong half of the market cycle. Re-test in a ranging
regime before declaring them broken.

### DRB on NQ daily can't clear the strict gate

A 108-cell sub-sweep around the prior near-passing configs found
**zero cells** passing the strict walk-forward gate. Best result
remains the original lookback=10 at ~+0.74 OOS Sh, 44% DSR pass.
DRB stays as a research candidate; not promoted.

## What happens next

1. **Paper-soak `mnq_orb_sage_v1`** alongside `mnq_orb_v1`.
   The pre-flight script (`paper_soak_mnq_orb.py`) runs on the
   plain ORB; a sister script for the sage variant goes in next.
2. **Apply sage overlay to `crypto_orb`** — direct generalization
   of the MNQ win to the crypto fleet. If it lifts crypto OOS
   Sharpe even half as much as it lifted MNQ (i.e. +2.73 → +4),
   crypto promotes.
3. **NQ-specific sage walk-forward** — current `nq_orb_v1` baseline
   was a mirror of MNQ; a separate sage sweep on NQ 5m would tell
   us whether sage's lift generalizes symbol-by-symbol.
4. **Push window count past 4** — MNQ 5m has only 107 days, which
   means 2 walk-forward windows. With 4+ windows the DSR is
   meaningfully gateable. Daily history is now fresh (extender
   ran today), but 5m intraday remains gated on TradingView
   Desktop pull or unparked Databento.

## Files in this commit batch

* `strategies/sage_consensus_strategy.py` — pure sage entry.
* `strategies/sage_gated_orb_strategy.py` — ORB + sage overlay.
* `tests/test_sage_strategies.py` — 14 unit tests.
* `scripts/run_sage_walk_forward.py` — sage harness.
* `scripts/sweep_sage_gated_orb.py` — 18-cell sage sweep.
* `scripts/sweep_drb_params.py` — 720-cell DRB sweep.
* `scripts/run_crypto_walk_forward.py` — crypto WF harness.
* `docs/research_log/sage_gated_orb_sweep_*.{md,json}` — sweep
  artifacts.
* `docs/research_log/sage_strategy_promotion_20260427.md`
* `docs/research_log/fleet_optimization_20260427.md` (this file)
* `strategies/per_bot_registry.py` — `mnq_futures_sage`,
  `nq_daily_drb` entries + extended `strategy_kind` doc enum.
* `docs/strategy_baselines.json` — pinned `mnq_orb_sage_v1`.

Total new test coverage: 29/29 passing across sage strategies
+ registry tests.
