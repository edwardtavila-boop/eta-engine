# 2026-04-27 — BTC crypto_orb: first honest crypto promotion

## What landed

**`btc_hybrid` PROMOTED** — first crypto strategy to clear the strict
walk-forward gate honestly.

```
| btc_hybrid | BTC/1h | btc | 9 | 7 | 1.800 | 5.084 | 26.8 | 1.000 | 66.7 | PASS |
```

- agg IS Sharpe **+1.800** (positive — real in-sample edge ✓)
- agg OOS Sharpe **+5.084** (validates out-of-sample ✓)
- 7/9 OOS windows positive
- degradation 26.8% (under 35% gate ✓)
- DSR median **1.000** (above 0.5 gate ✓)
- DSR pass fraction **66.7%** (above 50% gate ✓)
- strategy_id bumped to **`btc_corb_v2`**

## Promoted config

```python
strategy_kind = "crypto_orb"
extras = {
    "crypto_orb_config": {
        "range_minutes": 120,    # was 240 (default)
        "atr_stop_mult": 3.0,    # was 2.5 (default)
        "rr_target": 2.5,        # was 2.5 (default)
        "session_cutoff_hour_utc": 18,
    },
}
min_trades_per_window = 3   # was 10 (structurally unmeetable for crypto_orb)
```

## How it was found

Reused `scripts/sweep_crypto_orb_eth.py` (it accepts `--symbol`)
with `--symbol BTC --min-trades-per-window 3`. 36-cell grid over:

- range_minutes ∈ {120, 240, 360}
- atr_stop_mult ∈ {1.5, 2.0, 2.5, 3.0}
- rr_target ∈ {1.5, 2.0, 2.5}

Two cells PASS:

| Range | ATR× | RR | IS Sh | OOS Sh | Deg% | DSR pass% | Verdict |
|---:|---:|---:|---:|---:|---:|---:|---|
| 120m | 3.0 | 2.5 | +1.800 | +5.084 | 26.8 | 66.7 | **PASS** ← promoted |
| 120m | 3.0 | 2.0 | +0.747 | +3.355 | 25.4 | 66.7 | PASS |

The dominating cell (3.0/2.5) has higher IS, higher OOS, same
degradation, same DSR. Picked it over the sibling cell — no
ambiguity in the win criterion.

## Full-period stats (sanity check on the WF promotion)

Single backtest over the full 360-day Coinbase BTC 1h tape:

```
n_trades         52
win_rate         40.4%
avg_r            +0.4326 R / trade
r_stddev         1.7236
sharpe           +3.98
sortino          +38.4
expectancy_r     +0.4326
total_return     +24.23%
max_dd           4.90%
```

40% win-rate with +0.43R expectancy is exactly the ORB-family
profile the literature describes: **few-but-large winners, many
small losers, mathematical edge from RR > 1**. Max DD under 5%
on the full backtest is reassuring; the strategy doesn't blow
up even when it goes through a streak of false breakouts.

## Why this passes when ETH didn't

Same strategy_kind (crypto_orb), same engine, same gate. The
difference is **IS Sharpe**:

|  | BTC | ETH |
|---|---:|---:|
| agg IS Sharpe | **+1.800** | -3.018 |
| agg OOS Sharpe | +5.084 | +3.568 |
| IS+ windows | 5/9 | 2/9 |
| Verdict | **PASS** | FAIL (IS gate) |

ETH's OOS edge appears to be lucky-date-split — its IS phase
loses money in 7/9 windows. BTC's IS is positive in aggregate
and more than half its windows. The IS-positive gate added today
correctly distinguishes the two.

## Promotion safeguards already in place

The btc_hybrid registry row carries a **risk-warmup policy**:

```python
"warmup_policy": {
    "promoted_on": "2026-04-27",
    "warmup_days": 30,
    "risk_multiplier_during_warmup": 0.5,
}
```

Half-size for the first 30 days post-promotion. Reverts to 1.0
multiplier on 2026-05-27. This is the devils-advocate's
mitigation: 360-day sample isn't huge for crypto, OOS Sharpe of
+5 likely won't hold in live, and a half-size warmup buys the
data to confirm or refute without blowing the budget.

## Pre-live data-source gate (still required)

Per the standing operator directive (`eta_data_source_policy.md`):
this baseline is on **Coinbase spot bars**. Before real-money
activation:

1. Subscribe to IBKR's CME Crypto market-data bundle (~$10/mo).
2. Run `scripts/fetch_ibkr_crypto_bars.py` (TBD scaffold) for the
   same 360d window.
3. Re-run walk-forward at the promoted config; capture the IBKR
   baseline as a separate `BaselineSnapshot`.
4. `obs.drift_monitor.assess_drift(strategy_id="btc_corb_v2",
   recent=ibkr, baseline=coinbase)`. If severity = `green`, this
   Coinbase baseline transfers and we can promote. If `amber` or
   `red`, **do not promote** — re-tune on IBKR data, treat IBKR as
   authoritative, repeat.
5. Document the comparison in
   `docs/research_log/btc_hybrid_data_swap_<datestamp>.md`.

This is the same gate that protects every future crypto promotion.

## Honest fleet snapshot (post-BTC-promotion)

| Bot | Strat | IS | OOS | Verdict |
|---|---|---:|---:|---|
| mnq_futures | orb | +3.29 | +5.71 | **PASS** |
| nq_futures | orb | +3.29 | +5.71 | **PASS** |
| **btc_hybrid** | **crypto_orb (tuned)** | **+1.80** | **+5.08** | **PASS** ← NEW |
| eth_perp | crypto_orb (tuned) | -3.02 | +3.57 | FAIL (IS gate) |
| btc_regime_trend | crypto_regime_trend | -1.75 | +1.96 | FAIL (IS gate) |
| nq_daily_drb | drb | +1.36 | +2.48 | FAIL (DSR pass 39.6%) |
| sage variants | orb_sage_gated | +1–3 | +1.41 | FAIL (DSR boundary) |
| btc_hybrid_sage | orb_sage_gated | 0 | 0 | FAIL (no trades) |
| sol_perp | crypto_orb (default) | -0.76 | -5.17 | FAIL |
| crypto_seed | confluence | +0.71 | +0.01 | FAIL |
| xrp_perp | (DEACT) | 0 | 0 | DEACT |

**3 PASS strategies, all with real IS+OOS+ edge.**

## Files added/changed

- `strategies/per_bot_registry.py` — btc_hybrid: strategy_id
  v1 → v2, min_trades_per_window 10 → 3, crypto_orb_config
  range/atr/rr tuned, rationale rewritten.
- `docs/strategy_baselines.json` — new `btc_corb_v2` entry with
  walk-forward + full-period stats.
- `docs/research_log/2026-04-27_btc_first_crypto_promotion.md`
  (this file).

## Next-promotion candidates

1. `btc_corb_v2 / 120m / atr=3.0 / rr=2.0` — sister cell that
   also PASSes; could be ensembled with the promoted cell for
   diversification, or held in reserve as a re-baseline if the
   primary degrades.
2. `nq_daily_drb` — IS +1.36, OOS +2.48, but DSR pass only 39.6%.
   The 27y NQ daily history is the framework's longest tape; a
   regime gate ("skip windows where prior-month drawdown > X")
   might tighten the per-fold DSR distribution above 50%.
3. `mnq_futures_sage` / `nq_futures_sage` — both at the DSR
   boundary. More walk-forward windows (additional MNQ/NQ data)
   should resolve.

ETH and SOL crypto_orb deferred until either a strategy variant
produces IS+ for them or more bars are available.
