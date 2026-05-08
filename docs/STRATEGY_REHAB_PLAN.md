# Strategy Rehabilitation Plan — 2026-05-08

## Why this exists

Retiring is half the job. The corrected-engine audits flipped 14
bots from positive to negative, but the underlying *strategy
architectures* may still be valid — most retires came from
parameter mismatch or sample-size issues, not fundamental "no edge"
verdicts. This document lays out a concrete rehabilitation path for
each retired bot so they can earn their way back to the pin.

**Inclusion criteria for the active pin (post-2026-05-08 standard)**:
1. `expR_net > 0` on the corrected-engine audit
2. `n >= 30` trades (small samples earn no credit even at high Sharpe)
3. `split_half_sign_stable=True` (edge persists across both halves)
4. Either `sh_def > 0` (deflated Sharpe positive) OR sample density
   (`n / 624_days > 0.1` trades/day) so kaizen can verify on real fills
   within a reasonable window

A retired bot is rehabilitated when a re-audit shows it meets all
four criteria.

---

## Tier 1 — likely rehabbable with parameter tuning

### `rsi_mr_mnq`

**Why retired**: corrected-engine flip from +0.124 to -0.003 net,
split-stable became unstable. Sample 93 → 137. Pre-fix audit was
inflated by MNQ multiplier under-counting friction.

**Rehab thesis**: RSI/BB mean-reversion is a real edge in MNQ's range
days; current filters are too restrictive at threshold 25/75 with
strict rejection-candle requirement. Loosening should fire 2-3× more
often without hurting per-trade quality.

**Concrete tuning**:
- `rsi_oversold_threshold`: 25 → 28
- `rsi_overbought_threshold`: 75 → 72
- `min_volume_z`: 0.3 → 0.2
- Keep `require_rejection: True` (the discipline of rejection wick is
  what separates this from naive RSI)

**Re-audit gate**: rerun strict_gate on the tuned bot. Pin if
`expR_net > 0` AND `split_stable=True` AND `n >= 100`.

### `gc_sweep_reclaim`

**Why retired**: -0.179 net on 16 trades; the $100/pt gold multiplier
amplifies friction-per-R when stops are tight.

**Rehab thesis**: Gold has clean liquidity sweeps at NY-session
boundaries; the issue is per-trade $$ friction at full GC scale.

**Concrete tuning**:
- Switch to `mgc_sweep_reclaim` (Micro Gold, $10/pt) for 10× lower
  friction-per-R. Same strategy mechanic, different contract.
- OR for full GC: lift `rr_target` 2.5 → 3.5 to widen the
  win/friction ratio.

**Re-audit gate**: same. If MGC gives `n >= 30, expR_net > 0`, pin
MGC and retire GC permanently.

### `cl_sweep_reclaim`

**Why retired**: -0.052 net on 19 trades. Crude's $1000/pt
notional makes it a reasonable cap-fit but commission drag is high.

**Rehab thesis**: Energy markets are reflexive; sweep_reclaim
mechanic should work. Issue is sample size on the 1h timeframe.

**Concrete tuning**:
- Try 4h timeframe (instead of 1h) — fewer signals but cleaner
  trends. Crude's daily-range structure dominates over 1h noise.
- Relax `min_wick_pct`: 0.30 → 0.25
- Or switch to MCL (Micro Crude, $100/pt) for friction relief

**Re-audit gate**: same.

### `mes_sweep_reclaim`

**Why retired**: only 5 valid trades on corrected engine, -0.484 net.
Pre-fix sample was 34 trades; the corrected engine's stricter
friction calc filtered most out.

**Rehab thesis**: ES/MES have strong sweep_reclaim setups at
RTH-open and pre-close, but the current sweep_preset is calibrated
for MNQ which has different ATR structure.

**Concrete tuning**:
- Build `mes_sweep_preset` distinct from `mnq_sweep_preset`:
  - `level_lookback`: 48 → 24 (MES has tighter intraday structure)
  - `min_wick_pct`: 0.30 → 0.25
  - `atr_stop_mult`: 2.0 → 1.5 (MES is less volatile in absolute pts)
- Test on the existing 624-day MES1_1h dataset

**Re-audit gate**: same.

### `ym_sweep_reclaim`

**Why retired**: only 11 trades; couldn't fit budget cap on full YM.

**Rehab thesis**: YM has clean Dow-following sweeps at NY-open, but
$248k notional × $10k cap = sub-1-lot. The per_bot_budget_usd
override (just landed in `4e898ca`) lets a bot declare its own
$250k cap.

**Concrete fix**:
1. Add `extras["per_bot_budget_usd"] = 250000` to ym_sweep_reclaim
2. Re-audit — should now show >= 30 trades with the budget allowing
   1-contract entries
3. If audit shows positive net AND sufficient sample, re-pin.

**Faster path**: switch to MYM (Micro Dow, ~$25k notional) which fits
the default $10k cap with the existing `paper_futures_floor` lift.

---

## Tier 2 — rehabilitation needs data fixes

### `ng_sweep_reclaim`

**Why retired**: not actually retired in code, but pin-skipped
because registry comment flags `NG1_1h.csv` has 65 rollover-jump
bars (>5% adjacent-close jumps). The audit's Sharpe 8.31 number is
data-quality noise.

**Rehab thesis**: Strategy is sound; data is wrong.

**Concrete fix**:
1. Re-fetch NG via TWS with continuous front-month stitching that
   skips rollover gaps. The fetcher already has `--back-fetch` mode
   that handles this; just rerun for NG.
2. Verify `coverage` reports zero >5% jumps.
3. Re-audit — accept honest verdict.

### `mbt_sweep_reclaim` / `met_sweep_reclaim`

**Why "retired"**: 13 / 13 trades total in audit (was 0 pre-resample).
1h resample of MBT/MET 5m was the right move but exposed that the
sweep_preset doesn't match crypto-futures volatility on 1h.

**Rehab thesis**: MBT and MET have ample 5m bar data (109k bars
each). The 1h timeframe loses too much intraday structure to
detect sweeps.

**Concrete fix**: Switch these bots to 5m timeframe. They were
configured `timeframe="1h"` but the 5m source has 10× more bars
and the strategy reads liquidity sweeps which are 5m-scale events.

---

## Tier 3 — likely structural no-edge

### `vwap_mr_mnq`, `vwap_mr_nq`, `vwap_mr_btc`

**Why retired**: round-1, dispatch-collapse confirmed. Pre-fix
"winners" were stealing rsi_mr_mnq's signals. Once dispatched to
the vwap_reversion generator, edge collapsed.

**Rehab thesis**: vwap-reversion CAN work, but not on these
instruments + timeframes. The problem isn't params; it's that
the underlying mean-reversion thesis fails when MNQ/NQ/BTC trend
across the session VWAP for hours.

**Path**: leave retired. Re-evaluate only if:
- Different timeframe (4h, daily?) shows reversion behavior
- Different asset class (forex pairs?) where mean-reversion is structural

### `funding_rate_btc`

**Why retired**: 8481 trades, net negative. Plenty of sample to
rule out edge. The funding-rate momentum thesis is theoretically
sound but the noise floor on 1h crypto bars overwhelms the signal.

**Path**: leave retired. Crypto funding rates as a primary signal
are a research question, not a pin-eligible strategy.

### `btc_optimized`, `mnq_sweep_reclaim`, `btc_crypto_scalp`,
###  `btc_hybrid_sage`, `cross_asset_mnq`, `crypto_seed`,
###  `btc_ensemble_2of3`, `btc_zfade`, `nq_futures_sage`

**Why retired**: each had sample-size + sign-flip on corrected
engine. Most are different mechanics than the proven volume_profile
+ sweep_reclaim families.

**Path**: leave retired. Rehabilitation would require a different
strategy entirely, not parameter tuning.

---

## Operating procedure

1. **Don't touch anything for 7 days**: the new 7-bot pin needs real
   fills. Daily kaizen will surface real-fill verdicts.

2. **Day 7 review**: pick ONE Tier-1 candidate (probably
   `rsi_mr_mnq` since it was the top mid-tier survivor) and:
   - Apply the tuned parameters (no other changes)
   - Re-run strict-gate audit
   - Compare to pre-tune metrics
   - Pin if all 4 inclusion criteria pass; revert otherwise

3. **Don't tune more than ONE bot at a time** — multiple
   simultaneous changes confuse cause-and-effect when results come
   back. The goal is a controlled experiment per bot.

4. **MGC and MCL are easy wins** — they're micro-variants of GC and
   CL that already exist in the registry stub. Adding the
   `mgc_sweep_reclaim` / `mcl_sweep_reclaim` bot definitions and
   running them through audit costs almost nothing and unlocks the
   gold/crude exposure that the full-size variants couldn't deliver.

5. **NG re-fetch is a data-side fix** — operator can run the
   continuous-fetcher with rollover handling and re-audit
   independent of any strategy changes.

---

## Tracking table

| Bot | Status | Path | First gate (data/tune?) | Owner |
|---|---|---|---|---|
| rsi_mr_mnq | retired round-4 | Tune RSI thresholds | tune | TBD |
| gc_sweep_reclaim | retired round-4 | Switch to MGC OR widen RR | tune | TBD |
| cl_sweep_reclaim | retired round-4 | Try 4h or switch to MCL | tune | TBD |
| mes_sweep_reclaim | retired round-4 | Build MES-specific preset | tune | TBD |
| volume_profile_btc | retired round-4 | structurally hard; defer | n/a | n/a |
| ym_sweep_reclaim | unpinned (cap fit) | per_bot_budget_usd $250k OR switch to MYM | tune | TBD |
| ng_sweep_reclaim | unpinned (data quality) | Re-fetch with rollover handling | data | TBD |
| mbt/met_sweep_reclaim | unpinned (only 13 trades) | Switch to 5m timeframe | tune | TBD |
| MGC/MCL bot definitions | doesn't exist | Register clones of GC/CL | new | TBD |

---

## Why this matters

The fleet's job is to find edge. Edge only shows up under correct
measurement (the multiplier-fix umbrella) and adequate sample
(trade-density floors). Bots that fail today aren't necessarily
broken — they're broken AT THESE PARAMETERS. The disciplined path
back is one tune per bot per re-audit cycle, with the inclusion
criteria as the gate.

We don't grow the fleet by accumulating retires. We grow it by
graduating tuned strategies through the gate.
