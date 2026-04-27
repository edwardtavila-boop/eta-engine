# 2026-04-27 — Grid wiring fixes + open ETH/SOL OOS blow-up

## What landed (this iteration)

1. **DRB strategy_kind wired into the research grid.** `nq_daily_drb`
   was previously falling through to the confluence-scorer path and
   producing OOS Sharpe -4.59e+14. Now goes through `DRBStrategy`
   directly. Post-fix: agg OOS Sharpe +2.484, 26/53 positive windows.
   Real signal, still below the strict gate (DSR median 0.004).

2. **Crypto strategy variants wired** via a single
   `_build_crypto_strategy_factory` helper:
   `crypto_orb`, `crypto_trend`, `crypto_meanrev`, `crypto_scalp`,
   `grid` — all share the `maybe_enter` engine contract.

3. **Per-bot extras plumbed through.** `ResearchCell.extras` now
   forwards from `StrategyAssignment.extras`. Two shapes supported:
   nested (`crypto_orb_config: {"range_minutes": 240}`) and flat
   prefixed keys. `_safe_kwargs` drops registry keys that don't
   exist on the current strategy config so forward-looking fields
   (e.g. `session_cutoff_hour_utc`) don't crash the run.

## Latest grid (post-extras-fix)

| Bot | Sym/TF | Strat | OOS Sh | DSR pass% | Note |
|---|---|---|---:|---:|---|
| mnq_futures | MNQ1/5m | orb | +5.706 | 100.0 | **PASS** |
| nq_futures | NQ1/5m | orb | +5.706 | 100.0 | **PASS** |
| nq_daily_drb | NQ1/D | drb | +2.484 | 39.6 | FAIL (real signal) |
| btc_hybrid | BTC/1h | crypto_orb (range=240m) | +2.090 | 44.4 | FAIL (real signal) |
| eth_perp | ETH/1h | crypto_orb (range=240m) | -1.35e+15 | 55.6 | **BLOW-UP** |
| sol_perp | SOL/1h | crypto_orb (range=240m) | -1.35e+15 | 33.3 | **BLOW-UP** |
| crypto_seed | BTC/D | confluence-global | +0.014 | 37.5 | FAIL |

The PASS strategies (MNQ/NQ ORB) remain the only promoted baselines.

## Open: ETH/SOL OOS Sharpe -1.35e+15

ETH and SOL produce essentially the same magic number
(-1352705579044474 vs -1352705579044483). The IS Sharpe is sane
(+1.50 ETH, -0.76 SOL) and 5-9 OOS windows show positive Sharpe — so
*some* windows produce normal numbers. The aggregate gets dragged to
a 15-order-of-magnitude negative by one or more windows producing
+/-Inf or extremely large values that the aggregator doesn't filter.

BTC at the same kind+config produces sane numbers (+2.09), so it's
not the strategy or the config — it's specific to the ETH/SOL data
or to a numerical edge case the engine hits on those tapes.

### Hypothesis

A walk-forward window with zero OOS variance (flat closes for a
stretch) produces an OOS Sharpe of `mean / 0` → `±Inf`. The
aggregator then averages the windows numerically rather than
filtering Inf, propagating it. The 15-figure-precise final value
suggests the float underflow / overflow produces a deterministic
(but meaningless) number rather than a literal Inf or NaN.

### Next steps (deferred to whoever owns the engine path)

- In `WalkForwardEngine.aggregate_oos_sharpe`, filter windows whose
  OOS Sharpe is `±Inf` or NaN before computing the aggregate.
- Add an integration test: synthesise a 100-bar tape with all-equal
  closes in OOS, assert aggregate doesn't go to Inf.
- Cross-check that `fold_dsr_median` is computed from the same
  filtered set — currently ETH shows `DSR median 0.997` despite the
  blow-up, which means DSR and aggregate Sharpe disagree about
  what's a valid window.

## Most promising near-term promotion candidate

**btc_hybrid** with `strategy_kind=crypto_orb` and
`crypto_orb_config={range_minutes: 240}`:
- 9 walk-forward windows on 360 days of BTC 1h Coinbase bars.
- agg OOS Sharpe +2.090, IS Sharpe +0.186 (improvement from IS to
  OOS — strategy is more conservative IS, fires its real edge OOS).
- 6/9 windows positive OOS, 44.4% DSR pass — under the strict 50%
  gate but the closest a crypto bot has ever come.
- Operator directive: pre-live, re-fetch via IBKR + drift_check
  vs this Coinbase baseline.

If the next sweep tunes `atr_stop_mult` and `rr_target` per fold
(not per dataset), this is plausibly the third strategy ever to
clear the gate after MNQ ORB and NQ ORB.
