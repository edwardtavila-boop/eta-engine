# L2 Strategy Decision Memo

**Strategy:** `<bot_id>` (e.g. `mnq_book_imbalance_shadow`)
**Date:** YYYY-MM-DD
**Author:** <operator>
**Status transition:** `<current_status>` → `<recommended_status>`

---

## 1. One-line description

(e.g. "Top-3-level bid/ask qty imbalance, 3 consecutive snaps, ATR-based stops")

## 2. Falsification criteria (pre-committed)

| Criterion | Threshold | Time horizon |
|---|---|---|
| OOS sharpe (walk-forward test set) | `< 0` triggers retirement | 60 days |
| OOS n_trades | `< 30 over 30d` triggers retirement | 30 days |
| 14-day rolling sharpe | `< -0.5` triggers retirement | any 14d window |
| Confidence Brier score | `> 0.30` after n_trades=100 triggers retirement | continuous |
| Capture daemon outage | `> 2h` with overlay fail-CLOSED | per session |

## 3. Quant spec snapshot

- **Instrument:** `<symbol>` (e.g. MNQ JUN'26)
- **Decision frame:** depth snapshot (5s cadence)
- **Entry threshold:** `<entry_threshold>` (e.g. ratio >= 1.75)
- **Consecutive snaps:** `<consecutive_snaps>` (e.g. 3 = 15s of conviction)
- **Stop logic:** `entry ± atr_realized * atr_stop_mult`, floored at min_stop_ticks * tick_size
- **Target:** `stop_distance * rr_target` (e.g. RR 2:1)
- **Sizing:** hard-capped `max_qty_contracts` (1 in shadow, 2-3 in paper, op-decided in live)
- **Kill switches:**
  - spread_regime_filter PAUSE/STALE
  - trading_gate.check_pre_trade_gate (disk RED/CRITICAL or capture RED)
  - daily loss limit `$<max_daily_loss>`
  - gap-aware consecutive reset
  - zero-side classification fail-closed

## 4. Backtest evidence

### Walk-forward 70/30 split

| Metric | Train (70%) | Test (30%) |
|---|---|---|
| n_snapshots | `<…>` | `<…>` |
| n_trades | `<…>` | `<…>` |
| win_rate | `<…>` | `<…>` |
| sharpe_proxy | `<…>` | `<…>` |
| net P&L | `<…>` | `<…>` |
| sharpe CI 95% | `<lo, hi>` | `<lo, hi>` |

### Bootstrap confidence intervals

- Win rate 95% CI: `<lo, hi>`
- Sharpe 95% CI: `<lo, hi>`
- n_resamples: 1000

### Deflated sharpe (Bailey/Lopez de Prado)

- n_configs_searched: `<N>`
- observed sharpe: `<S>`
- deflated sharpe: `<DSR>`
- Comment: `<…>`

### Slippage realism

- Fill audit overall verdict: `<PASS|FAIL|INSUFFICIENT>`
- p90 slip (RTH MID): `<X ticks>` vs predicted 1 tick
- p90 slip (RTH OPEN): `<X ticks>`
- p90 slip (RTH CLOSE): `<X ticks>`
- p90 slip (ETH): `<X ticks>`

## 5. Red Team dissent (verbatim)

> "<paste exact Red Team output from the Firm review>"

### Attacks unresolved at this status

- `<list any FATAL/CRITICAL attacks not yet remediated>`

### Surviving risks the strategy must be monitored for

- `<list SURVIVABLE risks the Red Team flagged>`

## 6. Risk Manager sizing (paper / live only — skip in shadow)

- Per-trade risk: `<X% of account or $Y absolute>`
- Kelly fraction: `<…full Kelly → approved fraction…>`
- Daily loss limit: `<$Z>`
- Weekly loss limit: `<$Z>`
- Max drawdown kill switch: `<X% peak-to-trough>`
- Slippage buffer: `<size reduction>`
- What I've reduced vs Quant request: `<delta + reason>`

## 7. Macro / regime fit

- Current regime: `<growth × inflation × risk × vol>`
- Backtest regime breakdown: `<…>`
- Match or mismatch: `<…>`
- Scheduled catalysts (next 72h): `<…>`

## 8. Microstructure / execution

- Bid-ask spread cost: `<X ticks / Y bps>`
- Liquidity at intended size: `<adequate / constrained>`
- Order type: `<bracket / limit / market>`
- Latency requirement vs achievable: `<…>`
- Tape suitability for current session: `<…>`

## 9. PM decision

- **Decision:** GO / MODIFY / HOLD / KILL
- **If GO:** monitoring plan + checkpoint date (e.g. 7 days)
- **If MODIFY:** specific change + which stage re-runs
- **If HOLD:** what must change before re-review
- **If KILL:** post-mortem date + lessons captured

## 10. Override rationale (if PM overrode any agent)

- **Agent overridden:** `<…>`
- **Information PM has agent doesn't:** `<…>`
- **Empirical basis (not gut):** `<…>`
- **What outcome reverses the override:** `<…>`

## 11. Sign-off

- Operator signature: _____________________
- Date: __________________
- Next review date: __________________

---

**Archive in:** `var/eta_engine/decisions/<bot_id>_<YYYY-MM-DD>.md`
**Cross-reference:** Firm artifacts (Quant spec, Red Team dissent, Risk sizing,
Regime report, Execution report) in `var/eta_engine/firm/<bot_id>/`
