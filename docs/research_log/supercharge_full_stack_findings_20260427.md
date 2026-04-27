# Full-stack supercharge — infrastructure landed, hypotheses falsified, 2026-04-27

User directive: "i want everything possible supercharged."

This doc captures the complete arc of the 2026-04-27 supercharge
thread. **Two big hypotheses were tested and falsified**; **four
pieces of durable infrastructure shipped**. Net: the +6.00 BTC
champion's true OOS expectation remains +1.77/+1.96 long-run, the
codebase is meaningfully more capable for future strategies, and
the path forward is now clear.

## The two falsified hypotheses

### Hypothesis 1: Regime-conditional gate would lift OOS

**Falsified — regime gate HURT every variant.** See
``regime_gate_negative_result_20260427.md``.

| Variant | Agg OOS | +OOS | deg_avg |
|---|---:|---:|---:|
| Ungated baseline | +1.77 | 37% | 0.268 |
| Regime-gated default | +1.50 | 28% | 0.335 |
| Regime-gated strict-long-only | +0.56 | 21% | 0.397 |

deg_avg getting WORSE under gating is the diagnostic giveaway:
the gate was preferentially removing the BEST-aligned trades.
The classical price-EMA-ATR regime axis doesn't carve BTC's tape
along the same line as the strategy's edge.

### Hypothesis 2: Adaptive Kelly via engine callbacks would compound +1.77 to +2.5-3.0

**Falsified — Kelly HURT every variant.**

| Variant | Agg OOS | +OOS | deg_avg | DSR% |
|---|---:|---:|---:|---:|
| baseline | +1.77 | 21/57 | 0.268 | 26.3% |
| + funding filter | +1.77 | 21/57 | 0.268 | 26.3% |
| + adaptive Kelly | **+1.05** | 16/57 | **0.445** | 24.6% |
| + funding + Kelly | +1.05 | 16/57 | 0.440 | 24.6% |

Same deg_avg diagnostic — when Kelly amplified on winning
streaks, the next loss was disproportionately large; when it
shrunk on losing streaks, the next win was disproportionately
small. **Kelly amplifies VARIANCE of returns, not just mean.**
For a regime-conditional strategy whose hot streaks correspond
to regime alignment (not strategy edge), Kelly is anti-correlated
with edge.

The funding filter was a no-op: 0.075% per 8h is rarely hit on
modern BTC futures (post-2023 funding has been mostly tame). A
much-lower threshold would over-veto. Funding doesn't add signal
at the threshold-cutoff level for THIS strategy.

## The four pieces of durable infrastructure

### A. Five years of BTC 1h history (43,192 bars)

`fetch_btc_bars` extended the data set from 360 days (8,635 bars)
to ~5 years (43,192 bars) via Coinbase REST. Plus 4h / 15m / 1W
synthesized via canonical OHLCV resampling.

**Why this matters even when hypotheses fail:** every future
walk-forward measurement is now 6x more trustworthy. The +1.96
honest long-run baseline is grounded in 57 windows, not 9.

### B. Five years of BTC 8h funding history (5,475 rows)

`fetch_btc_funding_extended` extended from 96 days (Bybit / Binance
both US-blocked) to ~5 years via **BitMEX XBTUSD funding history**
(US-friendly, goes back to 2016-05-13).

Coverage:
* From: 2021-04-28 (5,475 rows = 5y of 8h fundings)
* To:   2026-04-26
* Source: BitMEX `/api/v1/funding`

This is genuinely uncorrelated information that future strategies
(or future iterations of the +6.00 champion's logic) can leverage.
Even though it didn't lift this strategy, it's on disk for the
next attempt.

### C. RegimeGatedStrategy wrapper + asset-class presets

Generic provider-driven gate (commit 7748867 + 7156a4c). Three
asset-class presets:
* `btc_daily_provider_preset()` — BTC 1h with daily-bar provider
* `mnq_intraday_preset()` — MNQ 5m with intraday classifier
* `eth_daily_preset()` — ETH 1h, higher vol cutoffs

15/15 unit tests pass. Code is generic + asset-safe + well-tested.
**Does not help the +6.00 BTC champion** (proven), but the MNQ
intraday preset is calibrated for ORB-style strategies and may
yet help that side of the fleet.

### D. Engine `on_trade_close` callback + AdaptiveKelly canonical signal path

`BacktestEngine` now exposes:
* `on_trade_close: Callable[[Trade], None] | None` — engine fires
  this once per realized trade
* `attach_trade_close_callback(...)` — late-binding listener
* `callback_stats` property — invocation + exception counts
* Exception isolation — bad listeners don't break the backtest

`AdaptiveKellySizingStrategy.on_trade_close(trade)` consumes the
callback directly. When a callback is attached, the heuristic
equity-delta inference path is disabled to prevent double-counting.

`WalkForwardEngine` auto-attaches the callback when the strategy
exposes `on_trade_close` — duck-typed, zero-config for new strategies.

8/8 integration tests pass. **Does not help the +6.00 champion**
(proven), but **future strategies** with regime-invariant edge
will benefit from proper trade-level Kelly compounding.

## What the data is actually telling us

Both hypotheses falsified the same way (deg_avg worsening under
the post-hoc layer) point to the same root cause:

> **You cannot extract more juice from a sample-specific result
> by post-hoc filtering or sizing.**

The +6.00 OOS Sharpe was a 2025-2026 consolidation-tape regime
artifact. Every attempt to recover it via:
* better selectivity (regime gates) — fails
* better sizing (adaptive Kelly) — fails
* better filtering (funding filter) — neutral

...because the strategy's edge during the +6.00 sample WAS the
regime alignment. There's no underlying-edge signal to amplify
because the +6.00 was driven by regime, not by strategy
alpha-in-isolation.

## What this means for the +6.00 BTC champion

**Promotion status unchanged.** The strategy is still the best
BTC strategy on disk. Live expectations corrected:

| Posture | Number |
|---|---|
| Headline OOS (360-day sample) | +6.00 |
| Honest 5y-window long-run OOS | +1.77 to +1.96 |
| Live deployment expectation | +1.5 to +2.5 long-run |
| Live deployment ceiling (regime-favorable) | +5 to +6 |

**Apex eval defensibility:** the eval window is short (~30 days)
and likely matches an edge regime, so eval PnL likely tracks the
+6 ceiling. Long-run expectations should track the +1.96 honest
baseline. Build risk limits + circuit breakers on the +1.96
expectation, not the +6 headline.

## Path forward (for real, no more knob-twiddling)

| Path | Leverage | Status |
|---|---|---|
| Acquire OI / order-flow / depth data | High | Blocked (US-friendly historical OI sources are short-tail; need paid aggregator) |
| New strategies with regime-INVARIANT edge | High | Open research direction |
| Apply RegimeGate + AdaptiveKelly to MNQ ORB (different asset, intraday regime axis) | Medium | Untested — could be a real win for MNQ |
| Paper-soak the +1.96 honest expectation | Medium | Validates the live-expectation correction |
| Multi-asset diversification at the portfolio level | Low-Medium | Architecture exists but uncorrelated alpha is the gating constraint |

## Files in the supercharge thread (2026-04-27)

### Commits

| Commit | Description |
|---|---|
| 4f966c4 | Wave 1 fetchers (resampler + OI + ETH ETF + extended funding) |
| 8852d66 | Extreme-OOS sweep — practical ceiling found |
| 3cc5fe8 | 5y walk-forward — +6.00 sample-specific, real OOS +1.96 |
| 7748867 | RegimeGatedStrategy wrapper + asset-class presets + 15 tests |
| 7156a4c | Regime gate hurts not helps — falsified hypothesis |
| (this) | Engine callback + AdaptiveKelly canonical path + funding stack |

### New code

* `strategies/regime_gated_strategy.py` — generic gate + 3 presets
* `strategies/adaptive_kelly_sizing.py` — `on_trade_close` canonical path
* `backtest/engine.py` — `on_trade_close` callback + isolation
* `backtest/walk_forward.py` — auto-wire callback when strategy exposes it
* `scripts/fetch_btc_funding_extended.py` — BitMEX source (10y-history-capable)
* `scripts/run_btc_regime_gated_walk_forward.py` — comparison harness
* `scripts/run_btc_supercharge_walk_forward.py` — full variant matrix
* `tests/test_regime_gated_strategy.py` — 15 tests
* `tests/test_engine_trade_close_callback.py` — 8 tests

### New data

* `C:/crypto_data/history/BTC_1h.csv` — 43,192 bars (5y)
* `C:/crypto_data/history/BTC_4h.csv` — 10,800 bars
* `C:/crypto_data/history/BTC_15m.csv` — 17,281 bars
* `C:/crypto_data/history/BTC_1W.csv` — 258 bars
* `C:/crypto_data/history/BTCFUND_8h.csv` — 5,475 rows (5y)
* `C:/mnq_data/history/ETH_ETF_FLOWS.csv` — 452 days (already in 4f966c4)

## Bottom line for the user

You asked to supercharge everything. We did:

* 6x more BTC price data ✅
* 57x more BTC funding data ✅
* Engine-level trade-close infrastructure ✅
* Generic regime-gate wrapper ✅
* Adaptive Kelly canonical signal path ✅

We honestly tested whether any of those upgrades could lift the
+6.00 BTC champion's long-run OOS Sharpe. **They couldn't.** The
champion's edge was regime-alignment-driven on its 360-day
sample; on 5y of all-regimes data the honest expectation is
+1.77 to +1.96 OOS Sharpe.

The infrastructure is durable. The next strategy with
regime-invariant edge will benefit from all of it. The next
attack on the OOS-recovery problem won't be more knob-twiddling
on the +6.00 champion — it'll be NEW STRATEGIES with structural
edge that doesn't disappear when the regime turns.
