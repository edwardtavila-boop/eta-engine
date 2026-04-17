# EVOLUTIONARY TRADING ALGO — Edge Rules

**Purpose.** Single source of truth for every trading rule the fleet uses.
Compiled from the bot docstrings + the brain layer (regime, htf_engine,
indicator_suite, confluence_scorer). Read this before changing any bot's
setup logic. If a rule here and a bot's docstring disagree, the docstring
is authoritative and **this file must be updated**.

**Last refresh.** 2026-04-17, roadmap v0.1.18, P1_BRAIN task `edge_doc`.

---

## 1. Philosophy

1. **No score, no trade.** Every entry must clear the confluence scorer's
   NO_TRADE threshold (total_score < 5 → reject, 5-6.9 → REDUCE size,
   ≥7 → TRADE). Scorer is in `core/confluence_scorer.py`.
2. **Regime is context, not a filter.** The regime classifier
   (`brain/regime.py`) reshapes feature weights via
   `brain/indicator_suite.py` rather than banning setups outright.
   CRISIS is the only regime that effectively halts trading by
   suppressing trend_bias weight and starving the scorer of total score.
3. **Top-down then bottom-up.** Daily + 4H bias from `brain/htf_engine.py`
   must not contradict the 5m/1m setup. Disagreement → 0 confluence
   contribution, not an auto-reject.
4. **Stops come before targets.** Every setup emits `stop_distance` in
   its signal meta. Position size is derived from stop, not from
   confidence. Target is 2R hard or trailing 1R from peak (MNQ/NQ) or
   liquidation-safe leverage cap (perps).
5. **Venue matters.** Futures (MNQ/NQ) use bracket orders via Tradovate.
   Perps (ETH/SOL/XRP/BTC-seed) use reduce-only IOC on Bybit v5.

## 2. Hierarchy: from macro to entry

```
     ┌─────────────────────────┐
     │  HTF Engine (Daily+4H)  │  brain/htf_engine.py
     │  bias ∈ {-1, 0, +1}     │  slope_sign + struct_sign
     └────────────┬────────────┘
                  │
     ┌────────────▼────────────┐
     │  Regime Classifier      │  brain/regime.py
     │  5 axes → 1 RegimeType  │  TRENDING / RANGING / HIGH_VOL /
     └────────────┬────────────┘  LOW_VOL / CRISIS / TRANSITION
                  │
     ┌────────────▼────────────┐
     │  Indicator Suite        │  brain/indicator_suite.py
     │  Regime-adaptive weights│  5 features rescaled per regime
     └────────────┬────────────┘
                  │
     ┌────────────▼────────────┐
     │  Confluence Scorer      │  core/confluence_scorer.py
     │  weighted → 0..10 score │  5→REDUCE, 7→TRADE, 9→75x cap
     └────────────┬────────────┘
                  │
     ┌────────────▼────────────┐
     │  Per-bot setups (5m/1m) │  bots/*/bot.py
     │  emit Signal            │  confidence >= 5, stop_distance set
     └────────────┬────────────┘
                  │
     ┌────────────▼────────────┐
     │  Smart Router + Venue   │  core/smart_router.py
     │  MARKET or POST_ONLY    │  MNQ→Tradovate, perps→Bybit
     └─────────────────────────┘
```

## 3. HTF engine (brain/htf_engine.py)

Produces `HtfBias` every daily close.

| Input          | Type                       | Source             |
|----------------|----------------------------|--------------------|
| daily_bars     | list[BarData]              | data_pipeline      |
| h4_bars        | list[BarData]              | data_pipeline      |
| daily_ema      | period=50 SMA-seeded EMA   | computed           |
| daily_struct   | HH_HL / LH_LL / NEUTRAL    | k=2 swing pivots   |
| h4_struct      | HH_HL / LH_LL / NEUTRAL    | k=2 swing pivots   |
| slope          | [0,1] (0.5 = flat)         | 2% rise → 1.0      |

**Composition rules** (in order — first applicable wins):

1. Fewer than `daily_ema_period` daily bars → bias=0, agreement=False.
2. Daily confluence: if slope_sign **and** daily_struct_sign are both
   non-zero, they must agree or daily_bias=0.
3. 4H gate: 4H must agree with daily_bias or be NEUTRAL. If 4H
   disagrees → final_bias=0.
4. `agreement` = daily_bias != 0 AND h4_sign != 0 AND same sign.

**Output consumption.** `HtfEngine.context_for_trend_bias()` emits a
dict matching what `features.trend_bias.TrendBiasFeature.evaluate`
expects — no adapter needed.

## 4. Regime classifier (brain/regime.py)

5-axis decision tree. Priority order, first match wins:

| Priority | Regime       | Rule                                          |
|---------:|--------------|-----------------------------------------------|
| 1        | CRISIS       | macro=="crisis" OR (vol>0.85 AND liq<0.2)     |
| 2        | HIGH_VOL     | vol>0.7 AND correlation>0.7                   |
| 3        | LOW_VOL      | vol<0.2 AND |trend|<0.2                       |
| 4        | TRENDING     | |trend|>0.5 AND 0.2 ≤ vol ≤ 0.7               |
| 5        | RANGING      | |trend|<0.3 AND 0.2 ≤ vol ≤ 0.5               |
| 6        | TRANSITION   | everything else                               |

Drift check: `detect_drift(recent, window=20)` returns True when the
latest regime differs from the mode of the trailing window — signal
for adaptation/RL update cadence.

## 5. Indicator suite (brain/indicator_suite.py)

The scorer's static `WEIGHT_TABLE` becomes regime-adaptive. Each row
sums to 10.0 so `total_score` stays on the [0, 10] scale.

| Regime      | trend_bias | vol_regime | funding_skew | onchain | sentiment |
|-------------|-----------:|-----------:|-------------:|--------:|----------:|
| TRENDING    |       4.0  |       1.5  |         2.0  |    1.5  |      1.0  |
| RANGING     |       1.5  |       3.0  |         2.0  |    1.5  |      2.0  |
| HIGH_VOL    |       2.0  |       1.0  |         3.0  |    2.0  |      2.0  |
| LOW_VOL     |       3.5  |       1.0  |         1.5  |    2.0  |      2.0  |
| CRISIS      |       1.0  |       1.0  |         3.0  |    2.5  |      2.5  |
| TRANSITION  |       3.0  |       2.0  |         2.0  |    1.5  |      1.5  |

Entry points: `weights_for(regime)`, `score_confluence_regime_aware(...)`,
`weighted_confluence_tuple(results, regime)`.

## 6. Per-bot setup catalog

### 6.1 MNQ futures bot (ENGINE tier)

Instrument: Micro E-mini Nasdaq-100. Tick $0.25, tick value $0.50,
point value $2.00. TF: 5m primary / 1m fills / 1s tick. Max 5x lev.

| Setup          | Trigger                                                   | Filter                                 | Confidence | Stop     |
|----------------|-----------------------------------------------------------|----------------------------------------|-----------:|----------|
| orb_breakout   | close > orb_high (or < orb_low)                           | volume > avg * 1.3                     |       7.0  | 1.5×ATR  |
| ema_pullback   | close touches EMA21 within 10 bps                         | regime=TRENDING, bar direction confirms|       6.5  | 1×ATR    |
| sweep_reclaim  | SweepResult.reclaim_confirmed                             | sweep direction defined                |       8.0  | close−level |
| mean_reversion | |close − VWAP| / ATR > 2.0                                | regime=RANGING                         |       6.0  | 1.2×ATR  |

Exit logic: hard stop at `-risk_per_trade_pct × R`, hard 2R target, or
trailing 1R from peak unrealized PnL per position id (see
`evaluate_exit`). Regime from ADX(14): ≥30 TRENDING, 20-30 TRANSITION,
<20 RANGING.

### 6.2 NQ futures bot (ENGINE tier, hybrid from MNQ)

Inherits MNQ's 4 setups. Overrides:
- **Confluence threshold raised** — NQ has 10× the tick value of MNQ,
  so every setup requires ≥1 more confluence point before routing.
- Position sizing recomputed from NQ point value.
- Trailing stop identical to MNQ.

### 6.3 ETH Perp bot (CASINO tier)

Instrument: ETHUSDT on Bybit v5. Leverage gated by confluence AND
liquidation distance:

| Confluence | Max leverage (confluence tier) |
|------------|-------------------------------:|
| 9.0+       |                           75x  |
| 7.0 - 8.9  |                           20x  |
| 5.0 - 6.9  |                           10x  |
| < 5.0      |                           REJECT |

Effective leverage = **min(confluence tier, liquidation-safe tier)`.
Liquidation-safe = `price / (3 × ATR × 1.20 + price × 0.005)`.

| Setup         | Trigger                                                 | Confidence                            |
|---------------|---------------------------------------------------------|---------------------------------------|
| trend_follow  | ADX≥25, ema_9 vs ema_21 direction, volume≥1.2×avg        | 6.0 + (ADX−25)/10 + vol_ratio (≤10)   |
| mean_revert   | close≥BB_upper + RSI>70 OR close≤BB_lower + RSI<30      | 6.5                                   |
| breakout      | ATR/avg_atr_50 < 0.75 (squeeze) AND bar range > 2×ATR   | 7.5                                   |

### 6.4 SOL Perp bot (CASINO tier, ETH inheritance)

Recalibrations for SOL's higher realized vol:
- Liquidation buffer **4.5 × ATR** (vs ETH's 3.0) — wider stop.
- Mean reversion uses **RSI 75/25** (vs 70/30).
- Breakout squeeze threshold **0.65** (vs 0.75) — SOL compresses less
  before explosive moves.
- Breakout range expansion **2.5 × ATR** (vs 2.0).

### 6.5 XRP Perp bot (CASINO tier, ETH inheritance)

Thinner book → tighter guards:
- **50x hard cap** even at 9+ confluence.
- Slippage buffer **0.008** (vs ETH's 0.005).
- trend_follow requires ADX **≥30** (vs 25) and vol_ratio **≥1.5** (vs 1.2).
- mean_revert uses RSI 75/25 like SOL.
- Router TODO: should select POST_ONLY for XRP; currently MARKET with
  the wider slippage buffer above.

### 6.6 BTC grid seed (SEED tier)

Geometric grid: `levels = low × (high/low)^(i/n)` for `i ∈ [0, n]`.
90% of equity is deployed across `n_levels` grid steps, 10% held in
reserve. Directional overlay activates when confluence ≥ 8.0 in the
bar's direction — grid continues unchanged and a single directional
position is opened alongside.

## 7. Cross-bot risk rails

Enforced by `core/risk_engine.py` and `core/kill_switch_runtime.py`:

1. **Per-trade risk:** size so that stop-out = `risk_per_trade_pct` ×
   current equity. Default 1% per config.
2. **Daily loss cap:** `daily_loss_cap_pct` (default 2.5%) — any trade
   that would breach is rejected; hitting it triggers pause.
3. **Max drawdown kill:** `max_dd_kill_pct` (default 8%) off peak
   equity — bot enters killed state, no new orders.
4. **Session filter:** `core/session_filter.py` blacks out CPI/FOMC/NFP
   windows for MNQ/NQ. Perps trade 24/7 but risk scales with funding.
5. **Portfolio correlation:** `core/portfolio_risk.py` caps aggregate
   exposure when cross-asset correlation > 0.8 (classifier regime
   HIGH_VOL feeds in here).
6. **Tail hedge:** `core/tail_hedge.py` opens BTC/ETH puts when
   `detect_drift` flips to CRISIS.

## 8. Feature catalog (reference)

5 features, each `compute()` returns [0, 1]:

| Feature         | Module                 | Raw input                      | Notes                                    |
|-----------------|------------------------|--------------------------------|------------------------------------------|
| trend_bias      | features/trend_bias    | daily_ema, h4_struct, bias     | HTF engine is the canonical provider     |
| vol_regime      | features/vol_regime    | atr_history, atr_current       | ATR percentile, sweet spot 0.3-0.8       |
| funding_skew    | features/funding_skew  | funding_history                | Cumulative 8h; extreme = opportunity     |
| onchain_delta   | features/onchain       | whale + netflow + addresses    | Async snapshot                           |
| sentiment       | features/sentiment     | galaxy, alt_rank, fear_greed   | Contrarian at <15 / >85                  |

## 9. Authoritative pointers

- `eta_engine/brain/htf_engine.py` — HTF composition logic.
- `eta_engine/brain/regime.py` — 5-axis classifier + drift detection.
- `eta_engine/brain/indicator_suite.py` — regime-weight table.
- `eta_engine/core/confluence_scorer.py` — score → leverage / signal.
- `eta_engine/bots/mnq/bot.py` — MNQ 4-setup engine (canonical ENGINE).
- `eta_engine/bots/eth_perp/bot.py` — CASINO-tier base (SOL/XRP inherit).
- `eta_engine/bots/crypto_seed/bot.py` — BTC grid + directional overlay.
- `eta_engine/core/risk_engine.py` — sizing, Kelly, drawdown kill.

## 10. Change log

| Date       | Roadmap | Change                                                                        |
|------------|---------|-------------------------------------------------------------------------------|
| 2026-04-17 | v0.1.18 | Initial consolidation from bot docstrings + brain layer. P1_BRAIN.edge_doc.   |
