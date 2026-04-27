# HTF conviction architecture — findings, 2026-04-27

User insight: "wouldn't these [Tier-4 signals] make more sense at
higher time frames... bigger confluence trades with a little
higher risk?"

## Summary up front

**The architectural intuition is correct, but the empirical
winner on BTC 1h with 360 days is still the simpler ETF-only
filter (+4.28 OOS).** The HTF directional gate produces +3.995
OOS — strong but slightly below. The size-scaling implementation
has an engine-interaction artifact that needs separate
investigation.

This entry documents the framework + the honest results so the
next research wave doesn't re-derive these findings.

## What was built

`strategies/htf_regime_oracle.py` — composes ETF + LTH + F&G +
macro + HTF-EMA into a single (direction, conviction) tuple.
Default weights ETF 0.30, HTF-EMA 0.25, LTH 0.15, macro 0.15,
F&G 0.15. Signed composite in [-1, +1]; conviction = abs value.
5-day EMA smoothing dampens single-day spikes.

`strategies/crypto_htf_conviction_strategy.py` — wraps
`crypto_regime_trend`. On each bar:
1. Update oracle's HTF EMA.
2. Always delegate to base (so its EMAs advance regardless of
   verdict — critical for state correctness).
3. Veto entry when direction is neutral OR conviction below
   `min_conviction_to_trade` OR base side disagrees with oracle.
4. Scale qty + risk_usd by `multiplier = base + (conviction -
   0.5) * gain`, capped to `[min_size_multiplier,
   max_size_multiplier]`.

14 unit tests cover oracle scoring, direction gating, conviction
sizing, multiplier caps. All pass.

## Walk-forward — BTC 1h, 90d/30d, 9 windows

Reference points:
* `crypto_regime_trend` (no filter): **+2.96 OOS**, 7/9, 91 trades
* `crypto_regime_trend` + ETF flow filter: **+4.28 OOS**, 8/9, 79 trades

HTF conviction variants:

| Variant | Agg OOS | +OOS | DSR_pass | Trades |
|---|---:|---:|---:|---:|
| **No conv scaling (mult=1.0)** | **+3.995** | 6/9 | 56% | 32 |
| Strict thresh=0.30, conv≥0.40 | +4.981 | 3/9 | 33% | 13 |
| Loose thresh=0.05, conv≥0.10 | +2.325 | 7/9 | 67% | 70 |
| Strict conv≥0.5, gain=2 | +0.212 | 1/9 | 11% | 4 |
| Linear gain=0.5 | **−145.17** | 6/9 | 56% | 32 |
| Linear gain=1.0 | **−63.48** | 6/9 | 56% | 32 |
| Linear gain=1.5 | **−36.04** | 6/9 | 56% | 32 |
| Linear gain=2.0 | **−22.43** | 6/9 | 56% | 32 |
| Aggressive gain=3 | **−57.49** | 5/9 | 56% | 32 |

## Three findings

### 1. HTF directional gating works (without scaling)

**+3.995 OOS** is solid — beats the no-filter baseline (+2.96) by
nearly +1.0 Sharpe. The 32-trade count is healthy. Per-window:

| W | IS Sh | OOS Sh | OOS_tr |
|---:|---:|---:|---:|
| 0 | 0.00 | **+5.61** | 2 |
| 1 | -0.30 | **+5.61** | 2 |
| 2 | -1.50 | +0.48 | 5 |
| 3 | +2.28 | 0.00 | 3 |
| 4 | +1.39 | +2.29 | 3 |
| 5 | +1.80 | **-3.24** | 6 |
| 6 | +1.42 | 0.00 | 4 |
| 7 | +2.77 | **+18.33** | 3 |
| 8 | +3.77 | **+6.87** | 4 |

W7 is a +18.33 outlier; W5 is the same -3.24 regime-shift loser.
8/9 non-negative OOS. Strict gate fails on deg_avg (W3 + W6 each
have 100% degradation = 0 OOS Sharpe). 

### 2. ETF-only filter still wins

ETF-only at +4.28 with 79 trades > HTF conviction at +3.995 with
32 trades. The HTF oracle IS more selective, but selectivity
costs trades faster than it raises per-trade edge on this sample.
This is consistent with the earlier finding that LTH and F&G
alone are negative-edge filters on BTC 1h — they're slow signals
that don't help at this granularity. Adding them to the ETF gate
just dilutes the dominant signal.

**Implication:** the user's intuition (HTF signals on HTF
timeframes) is architecturally correct. The lever to actually
benefit from it is to **build a daily-timeframe `regime_trend`
variant** where these slow signals are the right cadence, not
to layer them on 1h.

### 3. Size scaling produces negative Sharpes — engine artifact

Linear conviction scaling (gain > 0) produces wildly negative
agg OOS Sharpes (−22 to −145) on the SAME 32 trades. The math
is theoretically Sharpe-invariant (pnl_usd and risk_usd both
scale by mult, so pnl_r is unchanged). Yet the engine reports
huge negative Sharpes.

Likely cause: bar-level Sharpe computation interacts with
unrealized-PnL bookkeeping during multi-bar holding periods, or
DSR's small-sample correction blows up when single-trade $ swings
exceed a threshold. Needs separate engine-level investigation.

**Until that's fixed, do not enable conviction sizing in
production. The "no scaling" variant is the safe operating mode.**

## What to do next (in priority order)

### 1. Daily-timeframe regime_trend with HTF signals (highest leverage)

Build `crypto_regime_trend` on BTC daily (5 yrs / 30+ windows
available). At daily cadence, ETF + LTH + F&G + macro are AT
their natural granularity. The HTF oracle becomes the SAME-TF
regime detector instead of a HTF overlay. This is the right
match for the signal structure.

### 2. Investigate the size-scaling engine artifact

If conviction sizing is ever to be useful, the bar-level Sharpe
computation needs to be reviewed. Likely a PR worth doing
regardless — affects any strategy that varies position size
per trade.

### 3. Stick with ETF-only on 1h

Until the daily-TF variant lands, `btc_regime_trend_etf`
(+4.28 OOS) remains the strongest BTC research candidate. The
HTF conviction strategy is the framework for future work, not a
production replacement.

## Files in this commit

* `strategies/htf_regime_oracle.py` — oracle module.
* `strategies/crypto_htf_conviction_strategy.py` — wrapper.
* `tests/test_htf_conviction.py` — 14 unit tests.
* `docs/research_log/htf_conviction_findings_20260427.md` (this).

## Bottom line for the user

You called the architecture right: HTF signals → regime, LTF
strategy → execution, conviction → size. Built it. Tested it.
On BTC 1h with 360 days, the simpler ETF-only filter still wins
(+4.28 vs +3.995). Adding LTH + F&G + macro on top of ETF dilutes
more than it adds at this timeframe.

**The right move to actually realize the architectural win is to
take regime_trend down to BTC daily (5 yrs of data, 30+ walk-
forward windows) where these slow signals are at home.** The
framework is now in place to do that — same code path, different
data binding.
