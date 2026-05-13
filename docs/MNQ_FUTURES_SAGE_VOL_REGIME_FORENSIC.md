# mnq_futures_sage — Vol-Regime Forensic (the 100%-WR mystery solved)

**Date:** 2026-05-13 (wave-25o)
**Question:** why does mnq_futures_sage have 100% WR at qty<1 and 16.7% WR at qty=1?

**Answer:** The strategy's `vol_adjusted_sizing` halves position size in
high-vol regimes. High-vol setups are tight-stop, big-R, selective —
and they win at 100% in this sample. Normal-vol setups are wide-stop,
small-R, frequent — and they churn USD-negative even though net R is
slightly positive. **The strategy is working as designed; half its
trade book is just bad.**

---

## The split (109 records on VPS production_strict filter)

| Cohort | n | WR | avg R | sum R | avg USD | sum USD | sides | stops (implied) |
|---|---|---|---|---|---|---|---|---|
| **qty=1.0** (normal-vol) | 24 | 16.7% | −0.615 | −14.76 | −$30.85 | **−$740.50** | SELL 14 / BUY 10 | ~174 ticks (wide) |
| **qty=0.5** (high-vol, half-sized) | 24 | **100.0%** | +6.314 | +151.54 | +$20.23 | **+$485.50** | BUY 14 / SELL 10 | ~2 ticks (tight) |
| qty-untagged | 61 | mixed | mixed | mixed | mixed | (remainder) | | |

Stops inferred from `pnl / (qty * $5_per_tick) / R`. Wide normal-vol
stops produce small-R outcomes (tiny ticks moved relative to a big stop);
tight high-vol stops produce large-R outcomes (substantial ticks relative
to a small stop).

---

## What's actually happening

The supervisor's `_maybe_enter` calls `sweep_reclaim_strategy` (or the
futures-sage equivalent) with `vol_adjusted_sizing` enabled. The
sizing block computes:

```python
if recent_atr > atr_median * vol_high_threshold:
    size_mult *= vol_high_size_mult  # default 0.5
```

So high-vol bars → qty halved to 0.5. Then the supervisor's broker
submit accepts qty=0.5 in `paper_live` mode (the paper sim doesn't
enforce integer increments).

The strategy's SIGNAL GENERATION is unchanged across vol regimes —
both setups use the same sweep+reclaim mechanic. But the OUTCOME
distribution differs because:

1. **Tight stops + high vol = big-R winners or fast losers.** When
   conviction holds, you exit at a big multiple of risk. When wrong,
   the loss is small in dollars but a full R hit. In this 24-trade
   sample, conviction held 100% of the time.

2. **Wide stops + normal vol = bracket churn.** The price meanders;
   neither stop nor target gets a clean hit. The strategy exits on
   reclaim-window expiry or time-stop, producing tiny R values
   (R = ticks_moved / wide_stop_distance → small). Win/loss frequency
   is essentially coin-flip; commissions and slippage skew USD negative.

---

## Implications for the launch decision

### Option D — operator filter to high-vol regime only

Adjust the bot's config:

```python
# In sweep_reclaim_strategy SweepReclaimConfig (or its mnq_futures_sage
# variant):
vol_low_size_mult: float = 0.0  # SKIP normal-vol setups entirely
```

With this change, the strategy refuses to enter when ATR is below the
high-vol threshold. Only the qty<1 (high-vol) book remains, which has:
- 100% WR on 24 trades
- +$485 cum USD
- +6.3 avg R
- All in overnight + post-close hours (regime is stable, less news risk)

This is a **single config flip** away from a defensible launch candidate.

### What about sample size?

24 trades at 100% WR is small. Statistical confidence is real but
not high — the binomial 95% CI for "P(win) given 24/24 wins" is
roughly [0.86, 1.00], so the true hit rate is at least 86% with high
confidence. That's still a strong edge IF the high-vol regime
remains stable.

The right next step is a **paper-live soak of vol_low_size_mult=0.0**
for 2 weeks. If the high-vol-only book stays USD-positive on a 50+
trade sample, the bot is launch-ready.

---

## Caveat: I am NOT recommending the operator flip this for Monday

This is a real finding, not a Monday launch authorization. Reasons:

1. **24 trades is small.** Even at 100% WR, the strategy could have
   gotten lucky on a particular tape (post-CPI Asia session bounce,
   etc.). A bigger sample is required before risking real money.

2. **The forensic was done on 5 days of data.** Regime stability over
   weeks is what matters for a 30-day eval.

3. **mnq_futures_sage's broader history (1267 trades, +0.82R avg, 55% WR)**
   blends both vol regimes. The 100% / 16.7% split is from the recent
   wave-25 era. Whether the high-vol-only book holds up over wider
   historical windows is unknown.

4. **The config change needs testing.** Setting `vol_low_size_mult=0.0`
   has never been tested in paper mode. There could be edge cases
   (e.g. the bot fires zero trades for 3 weeks straight if vol stays
   low) that should be observed before going live.

---

## Recommended path forward

1. **Today (2026-05-13)**: ship this forensic; update `prop_launch_check`
   to surface per-qty-band stats in the launch-candidate report.
2. **Tonight**: set `vol_low_size_mult=0.0` for mnq_futures_sage in
   the bot config (NOT the global SweepReclaimConfig default).
3. **2 weeks (until 2026-05-27)**: let the supervisor paper-soak the
   filtered config. Daily `prop_launch_check` will show the post-filter
   USD trajectory.
4. **If high-vol-only book stays USD-positive with n≥50**: launch.
   The launch-candidate scan will surface the bot as qualifying.
5. **If not**: investigate further OR redesign the qty sizing entirely
   per `MES_V2_SIZING_FORENSIC.md` Fix C (constant-USD risk).

---

## What the wave-25 system did right

Caught the bug before it cost the operator the eval. The R-vs-USD
divergence was visible in the data; the qty asymmetry audit found
the pattern; the launch_candidate gate refused to designate any bot
as safe; the forensic isolated the actual mechanism. The discipline
of trusting the system's NO_GO bought time for this analysis.

---

## Cross-reference

- `docs/MES_V2_SIZING_FORENSIC.md` — first forensic in this thread
- `docs/FLEET_QTY_BUG_AUDIT.md` — fleet-wide extension
- `docs/LAUNCH_CANDIDATE_SCAN_2026_05_13.md` — today's launch verdict
- `docs/WAVE25_PROP_LAUNCH_OPS.md` — wave-25 architecture
- `eta_engine/strategies/sweep_reclaim_strategy.py` — vol_adjusted_sizing
  fields: `vol_adjusted_sizing`, `vol_high_threshold`, `vol_low_threshold`,
  `vol_high_size_mult`, `vol_low_size_mult`
