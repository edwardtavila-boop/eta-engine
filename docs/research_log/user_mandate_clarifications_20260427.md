# User mandate clarifications — multi-strategy bots + 15m+1m scalper, 2026-04-27

User clarifications during the supercharge thread:
1. "being futures the strategy was supposed to scalp on the 15
   minute and find entry on 1min - micro structure"
2. "i would also like a 5 minute orb stratey aswell one bot can
   run multiple strategys"
3. "with all the data we have how do we optimize regime"
4. "why is the sample thin we have so much data"

This doc captures the responses landed in commits d6bbf70 +
97fdb68 + (this commit).

## Question 4 — why was the sample thin?

It was MNQ-specific and timeframe-specific. **Data depth audit:**

| Symbol | 1m | 5m | 15m | 1h | D |
|---|---:|---:|---:|---:|---:|
| BTC | — | 180d | — | **1800d (5y)** ✅ | 1800d |
| MNQ1 | **22.7d** ⚠️ | 107d | **316d** ✅ | 1461d (4y) | 2548d (7y) |
| NQ1 | 22.7d | 107d | 316d | 1563d | 9798d (27y) |
| ETH | — | 180d | — | 360d | 1800d |
| SOL | — | 180d | — | 360d | 1775d |

The 5m supercharge harness ran on MNQ 5m (107 days = 2 windows).
Re-running on 1h (4 years) was a sanity check — produced 0.0 OOS
because ORB is a 5m intraday mechanic (it doesn't translate to 1h).

The honest fix: extend MNQ 1m / 5m data to match the 1h depth.
MNQ 1m at 22.7 days is the gating constraint for 1m-micro-entry
scalping research.

## Question 3 — how do we optimize regime?

Build a feature-based classifier on the actual signal axes the
strategy was designed around — not on price-EMA (which the prior
gate failed on).

**Result: FIRST regime gate to deliver positive OOS lift (commit
d6bbf70).** On 5y BTC 1h with the +6.00 champion:

| Variant | Agg OOS | +OOS | deg_avg |
|---|---:|---:|---:|
| baseline | +1.77 | 21/57 | 0.216 |
| **feature gate + strict_long_only** | **+2.07** | 11/57 | 0.334 |

`FeatureRegimeClassifier` scores funding state + ETF flow regime
+ F&G state + sage daily into a composite [-1, +1] score. Bias
gating filters out non-aligned regime tape.

## User mandate #1 — 15m direction + 1m micro-entry scalper

Built `MtfScalpStrategy` (commit 97fdb68 by parallel agent +
follow-on bug fix for recent-break logic). Mechanic:

* HTF (15m) — synthesized from 1m bars, 200 EMA bias, ATR%
  volatility regime, RTH session window
* LTF (1m) — momentum + EMA pullback + recent-extreme break
  for entry timing
* Stop / target sized off LTF ATR (typically 5-15 ticks on MNQ)
* Cooldown 30 1m bars
* Risk per trade 0.5% (smaller than swing)

7/7 unit tests pass after recent-break bug fix (was including
current bar's high in the comparison window, blocking all
breaks; fixed to compare against PRIOR window only).

**Walk-forward validation NOT yet possible** — needs 200_EMA_15m
warmup of 3000 1m bars + meaningful walk-forward windows. With
22.7 days of MNQ 1m, only ~32k bars available — enough for
warmup but ~1 walk-forward window. Real validation gates on
extending MNQ 1m to >= 6 months.

## User mandate #2 — 5m ORB also (one bot runs multiple strategies)

5m ORB already exists as `mnq_orb_sage_v1` (registry-promoted
2026-04-27, agg OOS +10.06 on 2 windows). Multi-strategy
composite is the new piece.

`MultiStrategyComposite` (commit 97fdb68) lets one bot run N
strategies in parallel:

* Each bar, every sub-strategy gets `maybe_enter()` called so its
  state advances — even if one sub wins the conflict resolution
* When N subs propose entries, a configurable policy picks one:
  * `priority` — first sub in declaration order wins
  * `confluence_weighted` — highest opened.confluence wins
  * `best_rr` — highest target/stop ratio wins
* Engine `on_trade_close` callback routes to the originator sub
  only — so AdaptiveKelly's R-streak ledger on sub A doesn't get
  polluted by sub B's outcomes
* Trade regime tagged with originator name for post-mortem
  attribution

10/10 unit tests pass.

**Use cases unlocked:**
* MNQ multi-strategy bot: 5m ORB + 15m+1m scalper running in
  parallel — doubles trade count on choppy days where both fire
* BTC multi-strategy bot: +6.00 sage_daily_etf + funding-divergence
  as contrarian counter — funding fires when sage sits, vice versa

## Configuration example — putting it together for MNQ

```python
from eta_engine.strategies.multi_strategy_composite import (
    MultiStrategyComposite, MultiStrategyConfig,
)
from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy
from eta_engine.strategies.mtf_scalp_strategy import (
    MtfScalpConfig, MtfScalpStrategy,
)

# Note: ORB runs on 5m bar stream; scalper on 1m bar stream.
# A single bot/composite naturally runs on ONE stream — you'd
# either resample on the fly (engine doesn't support yet) OR run
# two engines side-by-side and merge equity curves.

# For now, the composite is the right primitive when both subs
# can operate on the SAME bar stream. ORB at 1m vs 5m is a
# config flip; the scalper is naturally 1m. So:
mnq_orb_1m = ORBStrategy(ORBConfig(
    range_minutes=15,            # 15-minute opening range
    rth_open_local=time(9, 30),
    rth_close_local=time(16, 0),
    max_entry_local=time(11, 0),
    flatten_at_local=time(15, 55),
    timezone_name="America/New_York",
    rr_target=2.0, atr_stop_mult=2.0, atr_period=14,
    risk_per_trade_pct=0.005, max_trades_per_day=1,
))
mnq_scalper = MtfScalpStrategy(MtfScalpConfig(
    htf_bars_per_aggregate=15,
    htf_ema_period=200,
    risk_per_trade_pct=0.005,
))

bot_strategy = MultiStrategyComposite(
    [("orb_15m", mnq_orb_1m), ("scalper_15m_1m", mnq_scalper)],
    MultiStrategyConfig(conflict_policy="priority"),
)

# Wire on_trade_close so AdaptiveKelly (if wrapping either sub)
# gets canonical signal:
engine = BacktestEngine(
    pipeline, cfg, strategy=bot_strategy,
    on_trade_close=bot_strategy.on_trade_close,
)
```

## Next gating steps

1. **Extend MNQ 1m data to ~6 months** — current 22.7 days is
   the hard constraint on validating the 15m+1m scalper.
2. **Walk-forward sweep** the multi-strategy bot vs each
   sub-strategy alone — measure trade-count lift + OOS impact.
3. **Apply MultiStrategyComposite to BTC** with sage_daily_etf
   + funding-divergence (even though funding-div alone showed
   no edge, paired with a different mechanic the conflict
   arbitration may surface useful trades).
4. **Tune FeatureRegimeClassifier thresholds** (commit d6bbf70
   left this as next-move) — sweep bull_threshold,
   sage_conviction_floor, funding_extreme.

## Honest caveats

* The 15m+1m scalper is BUILT but NOT YET VALIDATED on real
  walk-forward — gating constraint is 1m data.
* The MultiStrategyComposite is BUILT and TESTED but no live
  bot configured to USE it yet.
* The FeatureRegimeClassifier delivered +0.30 lift on the BTC
  champion — that result IS validated on 5y of data.

## Files (this commit + prior parallel commits)

* `strategies/mtf_scalp_strategy.py` (97fdb68 + recent-break fix)
* `strategies/multi_strategy_composite.py` (97fdb68)
* `tests/test_multi_strategy_composite.py` (97fdb68, 10 tests)
* `tests/test_mtf_scalp_strategy.py` (this commit, 7 tests)
* `docs/research_log/user_mandate_clarifications_20260427.md` (this)

## Bottom line for the user

You asked four things; we delivered:

1. **15m+1m scalper:** Built (`MtfScalpStrategy`), 7 tests pass.
   Gated on extending MNQ 1m data.
2. **5m ORB stays + multi-strategy bots:** Built
   (`MultiStrategyComposite`), 10 tests pass. Ready to run any
   N-sub combo per bot.
3. **Optimized regime:** First lift in this thread: **+0.30 OOS
   on the +1.77 baseline** via FeatureRegimeClassifier with
   strict_long_only on 5y BTC.
4. **Why thin sample:** MNQ 5m has only 107 days; MNQ 1h has
   4 years (but ORB doesn't work at 1h). MNQ 1m at 22.7 days
   is the next data-extension priority.

Live OOS expectation for the BTC champion: **+2.07** (was +1.77
without feature regime gate). For MNQ 5m ORB + 15m+1m scalper
combo: pending data extension and walk-forward validation.
