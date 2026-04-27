# 2026-04-27 — Option A applied, ETH promotion withdrawn (IS Sharpe negative)

## What was done

Operator selected Option A from the prior research log: relax the
walk-forward gate's `all_met` requirement so selective crypto
strategies (which fire fewer trades per OOS window than MNQ/NQ
ORB on 5m bars) aren't structurally locked out.

### Implemented

1. **`WalkForwardConfig.min_trades_met_fraction`** (default 0.8).
   Was previously a hard `all(...)`; now configurable. 1.0 reproduces
   the original strict behavior.

2. **`per_bot_registry.eth_perp`**:
   - `strategy_id`: `eth_corb_v1` → `eth_corb_v2`
   - `min_trades_per_window`: 10 → 3
   - `crypto_orb_config.range_minutes`: 240 → 120
   - `crypto_orb_config.atr_stop_mult`: (default 2.5) → explicit 2.5
   - `crypto_orb_config.rr_target`: (default 2.5) → explicit 2.5

After the registry change ETH crypto_orb passed the strict gate
with verdict **PASS** in the research grid:
agg OOS +3.568, deg 11.1%, DSR med 1.000, 77.8% fold pass.

## Why it was withdrawn

Per-window inspection showed:

```
win 0: IS -13.043  OOS  +7.657  deg 0.0
win 1: IS  -6.325  OOS  +5.892  deg 0.0
win 2: IS  -3.689  OOS  +9.109  deg 0.0
win 3: IS  -0.987  OOS  +0.000  deg 0.0
win 4: IS  -2.115  OOS  +8.301  deg 0.0
win 5: IS  -0.617  OOS -24.290  deg 1.0
win 6: IS  -1.507  OOS +14.741  deg 0.0
win 7: IS  +0.490  OOS  +4.811  deg 0.0
win 8: IS  +0.636  OOS  +5.892  deg 0.0
```

IS Sharpe is **negative in 7/9 windows**. The aggregate IS Sharpe
is **-3.018**. The strategy lost money on its in-sample data in
nearly every window.

A walk-forward validation is supposed to confirm IS+ AND OOS+.
A strategy whose IS phase consistently loses money cannot be
honestly trusted by an OOS pass — that's likely lucky-date-split,
not validated edge. (Compare MNQ ORB: IS +3.292, OOS +5.706 —
real edge in both phases.)

### The gate hole

`backtest.walk_forward._degradation` returns 0 when
`is_sharpe <= 0 and oos_sharpe >= is_sharpe`. That's defensible
as a *measure* (you can't degrade by going from negative to less-
negative), but it lets IS-negative strategies through the
`deg_avg < 0.35` gate as if they had no degradation.

The strict gate's other criteria (DSR, fold pass-fraction) all
operate on aggregates that hide IS sign. None of them ask the
load-bearing question: *does the strategy work in IS?*

## What was added to the gate

`legacy_gate` now also requires `agg_is_sharpe > 0`. Catches
exactly this case. Verdict for ETH crypto_orb (research config)
flipped from PASS back to FAIL.

```python
# Before:
legacy_gate = dsr > 0.5 and deg_avg < 0.35 and all_met
# After:
legacy_gate = dsr > 0.5 and deg_avg < 0.35 and all_met and is_positive
```

## What stays in

The two engine improvements from this iteration are valid
regardless of the ETH outcome and are kept:

1. `min_trades_met_fraction = 0.8` (was strict `all`). Selective
   strategies aren't structurally locked out.
2. `is_positive` requirement on `legacy_gate`. Closes the
   IS-negative hole.

The eth_perp registry retains the tuned `range=120m` config —
real research finding (deg 11.1% vs default 44.4%), just not
promotable until the IS issue is addressed.

## Honest fleet snapshot (post-gate-fix)

| Bot | Strat | Agg IS | Agg OOS | Verdict |
|---|---|---:|---:|---|
| mnq_futures | orb | +3.292 | +5.706 | **PASS** |
| nq_futures | orb | +3.292 | +5.706 | **PASS** |
| mnq_futures_sage | orb_sage_gated | +1.163 | +1.413 | FAIL (DSR boundary) |
| nq_futures_sage | orb_sage_gated | +3.435 | +1.413 | FAIL (DSR boundary) |
| nq_daily_drb | drb | +1.361 | +2.484 | FAIL (DSR pass 39.6%) |
| btc_hybrid | crypto_orb | +0.186 | +2.090 | FAIL |
| eth_perp | crypto_orb (tuned) | -3.018 | +3.568 | FAIL (IS negative) |
| sol_perp | crypto_orb | -0.760 | -5.171 | FAIL |
| crypto_seed | confluence-global | +0.708 | +0.014 | FAIL |
| btc_hybrid_sage | orb_sage_gated | 0.000 | 0.000 | FAIL (no trades) |
| xrp_perp | (DEACT) | 0.000 | 0.000 | DEACT |

## Why this isn't a setback

The gate caught exactly what it should catch. Two PASS strategies
remain. The framework is now MORE trustworthy than it was an hour
ago: a real validation hole was found and closed.

## Next-promotion candidates

In honest order:
1. **`mnq_futures_sage` / `nq_futures_sage`** — both at the DSR
   boundary (50% pass, fold median 0.500). Need more walk-forward
   windows to push above the threshold; currently only 2 windows
   on 107 days of data. Re-evaluate after 6 months of additional
   MNQ/NQ data lands.
2. **`btc_hybrid` (crypto_orb)** — agg IS +0.186 (positive ✓),
   agg OOS +2.090, deg 28.7%, DSR pass 44.4%. Closest crypto bot
   to a real PASS. Per-fold tuning of the same parameter sweep
   may push it through.
3. **`nq_daily_drb`** — IS +1.361, OOS +2.484, but DSR pass only
   39.6%. The 27y NQ daily history is the framework's longest
   tape; a regime gate ("skip windows where prior-month drawdown
   > X") might tighten the per-fold DSR distribution.

ETH crypto_orb deferred until either (a) a strategy variant
produces IS+, or (b) more ETH bars are available to reduce the
per-window IS variance.
