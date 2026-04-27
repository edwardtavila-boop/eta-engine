# Regime gate hurts, not helps — honest negative result, 2026-04-27

After the 5-year walk-forward (commit 3cc5fe8) showed the +6.00
BTC champion was sample-specific (real OOS +1.96 across 57
windows on 5y), the next-step hypothesis was: gate firings on
HTF regime classification, exclude unfavorable regimes, recover
toward +3-4 OOS.

**Hypothesis falsified.** The regime gate, as built and
calibrated, makes the strategy WORSE on 5y data, not better.

## Setup

Built ``RegimeGatedStrategy`` (commit 7748867) — generic
provider-driven gate that wraps any sub-strategy. Initial LTF-
stream classifier had a warmup-bound bug (200d×24h = 4800-bar
warmup vs 90-day windows = 2160 bars), so extended the wrapper
to accept a pre-computed daily-bar classifier provider via
``attach_regime_provider``. New preset ``btc_daily_provider_preset()``
pairs with this provider mode.

Daily classifier config (default):
- 50/200 EMA on daily bars
- 220-bar warmup (~7 months)
- 3% trend-distance cutoff, 2% ATR-pct cutoff

## Daily regime distribution on 1800 BTC daily bars

| Regime | Bars | % |
|---|---:|---:|
| trending | 1,414 | 78.6% |
| ranging | 0 | 0.0% |
| volatile | 386 | 21.4% |

**First red flag:** zero ranging bars. The classifier puts almost
everything into "trending" because BTC daily ranges routinely exceed
3% from the slow EMA. The gate effectively only excludes the 21%
of days the classifier deems "volatile" — and those days turn out
to contain positive-OOS firings, not negative ones.

## Walk-forward comparison (90/30 windows, 5y BTC 1h)

| Variant | Agg OOS | +OOS | deg_avg | DSR pass | Gate |
|---|---:|---:|---:|---:|:---:|
| **Ungated baseline** | **+1.77** | 21/57 (37%) | **0.268** | 26% | FAIL |
| Regime-gated (default) | +1.50 | 16/57 (28%) | 0.335 | 21% | FAIL |
| Regime-gated + strict_long_only | +0.56 | 12/57 (21%) | 0.397 | 9% | FAIL |

**Every gate variant degrades every metric.** And worse:

* deg_avg gets WORSE with gating (0.27 → 0.34 → 0.40), meaning
  the gate is preferentially removing the trades whose IS-OOS
  agreement was best. That's the opposite of selectivity.
* Positive-OOS folds drop from 21 to 16 to 12 — the gate veto
  rate aligns with negative folds, not positive ones.
* DSR pass fraction collapses 26% → 9%.

## Why the hypothesis was wrong

I assumed the +6.00 champion's edge was concentrated in "trending
bull, low-vol consolidation" tape and the gate would exclude
volatile drawdown / ranging chop. The data says otherwise:

1. **The classical 3-state regime partition (trending / ranging /
   volatile) doesn't carve BTC's historical tape along the same
   axis as the strategy's edge.** The classifier groups almost
   everything as "trending"; the strategy's good and bad windows
   are both inside the "trending" bucket.

2. **The 21% labeled "volatile" tape contained positive-OOS
   firings.** Removing them dropped the agg OOS — they were the
   "moonshot" trades the strategy was designed to catch (volatility
   expansion phases like Jan 2024 ETF launch, March 2024
   blow-off, etc.).

3. **Strict long-only made it even worse.** When I forced LONG
   bias to also match, the agg OOS dropped to +0.56. This means
   the strategy was making good SHORT entries during bear-bias
   tape that the gate now vetoes. The +6.00 champion is NOT a
   pure long-only system on 5y — it correctly takes shorts in
   bear-trending tape, and the gate breaks that.

4. **deg_avg getting worse is the diagnostic giveaway.** When a
   gate is anti-correlated with edge, it removes the best trades
   first (because those trades happen in regimes the classifier
   labels "wrong"). When deg_avg goes UP under gating, the gate
   is taking from the winners' pile.

## What this rules out

The naive HTF-EMA + ATR + slope regime classifier does NOT carry
the right information to discriminate the +6.00 strategy's edge
regime from its no-edge regime on 5-year BTC 1h data. That means:

* Tighter / wider trend-distance thresholds won't fix it — the
  axis is wrong, not the cutoffs.
* Adding more allowed_regimes won't help — at zero ranging bars,
  there's nothing to add back.
* Asset-class preset tweaks (eth_daily_preset etc.) won't help
  for ETH either — same partition, same blind spot.

## What the data is actually telling us

The +6.00 champion's edge regime is NOT a simple price-EMA-ATR
regime. It's likely defined by a feature we have NOT yet wired:

1. **Funding-rate state** — high-positive funding (overheated longs)
   should mean-revert; high-negative funding (capitulated shorts)
   should bounce. We don't yet have funding history (Bybit + Binance
   both US-geo-blocked); fetcher work is parked pending a non-
   blocked source (OKX / BitMEX / Coinglass).

2. **Volume profile** — accumulation phases vs distribution phases
   show on the volume axis, not the price-derived axis.

3. **ETF flow regime** — the strategy already uses ETF flow as a
   filter. Maybe the edge regime is "ETF flow positive AND
   directionally consistent for N days" — a temporal feature we
   don't currently capture.

4. **Cross-asset tape** — DXY trend, QQQ trend, gold. We have a
   ``MacroTailwindProvider`` but it's not currently active in the
   +6.00 champion.

## Decision

**Park the simple HTF regime gate.** It doesn't carry signal for
this strategy on this data. Keep the wrapper code (it's cleanly
generic, well-tested, may help OTHER strategies — particularly
MNQ ORB where the regime axis is intraday range vs trend, which
DOES align with that strategy's edge — see ``mnq_intraday_preset``).

For BTC, the path to recovering toward +6.00 is NOT regime gating
on price-EMA features. The honest paths forward:

| Path | Effort | Expected lift |
|---|---|---|
| Wire funding-rate provider (need non-US-blocked source) | Medium | High — funding is uncorrelated with price |
| Wire OI provider (same source constraint) | Medium | High — OI changes are leverage-direction signal |
| Add temporal-ETF-flow feature (sustained inflow/outflow) | Low | Medium |
| Train a regime classifier directly on success/failure windows | High | Unknown — but the right approach |
| Engine trade-close PnL callbacks for proper Adaptive Kelly | Medium | Compounding lift on whatever base we have |

The last bullet is interesting: **even if we can't lift the OOS
Sharpe, proper Adaptive Kelly sizing would compound the existing
+1.77 OOS more aggressively in winning streaks.** That's
multiplicative not additive — a +1.77 strategy with 1.5x compounding
in win streaks could realistically deliver +2.5-3.0 effective
Sharpe at the equity-curve level.

## Files in this commit

* ``strategies/regime_gated_strategy.py`` — added provider-driven
  gate (``attach_regime_provider``) + ``btc_daily_provider_preset``;
  marked LTF-stream ``btc_daily_preset`` as deprecated for WF use
  (warmup-bound).
* ``scripts/run_btc_regime_gated_walk_forward.py`` — comparison
  harness (ungated vs gated, with --compare and --strict-long-only).
* ``docs/research_log/regime_gate_negative_result_20260427.md``
  (this).

## Bottom line for the user

You asked for "supercharge everything." Part of supercharging is
falsifying hypotheses fast. We hypothesized that HTF regime
gating would lift the +1.96 baseline back toward +3-4. **That
hypothesis is false** on this data with this classifier.

The +1.77 / +1.96 OOS baseline is the honest expectation. The
strategy works; it's just not 6.00 on average. The path forward
is NEW INFORMATION (funding, OI, sustained-flow features), not
better filtering of the data we already have.

Promotion status of ``btc_sage_daily_etf_v1`` is unchanged — it's
still the best BTC strategy on disk. Live expectation: +1.5 to
+2.5 OOS Sharpe long-run, with occasional regime-favorable
periods (like the 9-window 360-day sample) producing +5 to +6
runs.
