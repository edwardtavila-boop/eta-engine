# Macro confluence findings — 2026-04-27

User insight: "no one factor truly moves [BTC] alone — they
interact." Drivers listed: ETF flows, macro liquidity, leverage /
funding, on-chain LTH supply, sentiment, time-of-day session.

This entry captures the framework built to test that thesis + the
honest results across all stackable filter combinations.

## What was built

`strategies/crypto_macro_confluence_strategy.py` composes the
+2.96 OOS regime_trend baseline with eight opt-in filters:

| Filter | Tier | Status |
|---|:---:|---|
| A. HTF EMA alignment | 1 | active (OHLCV) |
| B. Time-of-day window | 1 | active (bar.ts) |
| C. Volatility regime band | 1 | active (ATR percentile) |
| D. BTC-ETH correlation | 2 | active (ETH OHLCV provider) |
| E. Funding rate filter | 3 | active (BTCFUND_8h provider) |
| F. Macro tailwind DXY+SPY | 2 | active (Yahoo daily provider) |
| G. ETF flow alignment | 4 | placeholder — needs fetcher |
| H. On-chain LTH supply | 4 | placeholder — needs fetcher |

Each filter is independently toggled. Filter veto rolls back the
base strategy's cooldown so a later confluent bar can fire.

Three concrete providers (`macro_confluence_providers.py`):

* **EthAlignmentProvider** — pre-computes ETH regime EMA at every
  bar timestamp; lookup returns +1/-1/0 score.
* **FundingRateProvider** — reads BTCFUND_8h CSV, returns most
  recent funding rate at-or-before bar.ts.
* **MacroTailwindProvider** — DXY + SPY 5-day slopes, composite
  [-1, +1] score (weighted -DXY + +SPY).

## Walk-forward results (BTC 1h, 90d/30d, 9 windows)

| Variant | Agg OOS | +OOS | Trades | Δ vs baseline |
|---|---:|---:|---:|---:|
| **Baseline (no filters)** | **+2.96** | **7/9** | **91** | — |
| **A+F: HTF + macro** | **+3.11** | 4/9 | 53 | **+0.15** ✓ |
| D only: ETH alignment | +2.70 | 7/9 | 81 | -0.26 |
| A+D+F: HTF+ETH+macro | +2.02 | 3/9 | 54 | -0.94 |
| C only: vol band | +1.76 | 7/9 | 99 | -1.20 |
| ALL active filters | +1.60 | 4/9 | 38 | -1.36 |
| A only: HTF=800 | +1.40 | 7/9 | 95 | -1.56 |
| B only: hours 13-16 UTC | +1.11 | 5/9 | 99 | -1.85 |
| A+B: HTF + hours | -0.17 | 3/9 | 84 | -3.13 |
| F only: macro >=0.2 | -1.48 | 5/9 | 60 | -4.44 |

## Conclusions

### 1. Filters tested don't compound the way the thesis predicts

The user's thesis (multiple uncorrelated drivers should compound
into a stronger signal) is sound in principle, but in practice:

* Most single filters **subtract** edge. They cut trades faster
  than they cut losers.
* The only winning combination, A+F (HTF + macro tailwind), beats
  the baseline by +0.15 Sharpe — but with 53 trades vs 91. The
  uplift is within walk-forward noise.

### 2. The baseline regime_trend already captures the dominant signal

The 2-EMA regime gate (close > 100 EMA + pullback to 21) is doing
most of the work. Layering additional rules on top costs more good
trades than it saves. Same finding as the EMA-stack experiment.

### 3. The drivers that WOULD move the needle aren't in our data

The user's listed factors that we DON'T have proper feeds for:

* **ETF net flows** (IBIT, FBTC daily creation/redemption) —
  arguably the single biggest driver in 2025-2026 per the user's
  read. No provider yet.
* **On-chain LTH supply / exchange reserves** — the "diamond hands
  vs distribution" signal. No provider yet.
* **Sentiment (Google Trends, social, regulatory news)** — high-
  variance but real. No provider yet.
* **Equity correlation regime** — when BTC-SPY correlation flips
  (risk-off), the regime gate is fighting the wrong battle. We
  have SPY data but no correlation-regime detector.

These are Tier 4 in the matrix above. The framework supports them
via opt-in providers; the strategy code is unchanged when adding
them.

## What to do next

### Tier 4 data acquisition (highest expected leverage)

1. **ETF flow fetcher** — daily IBIT (BlackRock) net flow + total
   cumulative AUM. Source: BlackRock ETF holdings page or a
   structured aggregator like SoSoValue. Probably the single
   biggest unlocked signal.
2. **On-chain LTH supply** — Glassnode-style metric (long-term
   holder supply % of circulating). Could be approximated from
   public block-explorer APIs, but a proper feed is preferred.
3. **Sentiment** — Google Trends API for "buy bitcoin", news
   sentiment via a free-tier news aggregator.

Each of these slots into `MacroConfluenceConfig` as a one-line
toggle once the provider exists.

### Longer data spans (medium leverage, low cost)

Re-run macro confluence on BTC daily (1800 bars / 5 years vs the
current 360 days / 9 windows). 30+ windows would crush the
single-outlier risk that's keeping every strategy's `deg_avg`
above the gate's 0.35 cap.

### Don't pursue further (won't move the needle)

Based on the negative findings:
* More EMA stack variants — established that strict ordering hurts.
* More vol-regime tuning — variant C is mildly negative regardless
  of band.
* More time-of-day filters — variant B is consistently negative on
  this data.

## Files in this commit batch

* `strategies/crypto_macro_confluence_strategy.py`
* `strategies/macro_confluence_providers.py`
* `tests/test_crypto_macro_confluence.py` (15 tests)
* `docs/research_log/macro_confluence_findings_20260427.md` (this)

## Bottom line for the user

Your insight that BTC has multiple drivers is correct. The
framework now supports combining all of them. **But the data we
currently have on disk is not enough to push past the +2.96 OOS
baseline by a statistically meaningful margin.** The next real
upgrade is wiring ETF flow + on-chain feeds — not more filters
on the existing OHLCV data.

The crypto_macro_confluence_strategy stays in the codebase as the
framework. Once Tier 4 data is fetched, adding it is a 1-line
config change and a 10-line provider class. The bones are ready.
