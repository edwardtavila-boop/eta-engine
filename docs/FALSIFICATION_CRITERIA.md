# Pre-Committed Falsification Criteria — 8 Active Bots

- **Date created:** 2026-05-12
- **Author:** jarvis_strategy_supervisor / The Firm
- **Allocation source:** `var/eta_engine/state/capital_allocation.json` (VPS snapshot 2026-05-12)
- **Pool:** $100k paper-futures capital
- **Decision rule:** any "Retire if" trigger fires → operator commits to retiring the bot within 24 hours of the trigger, no debate. The point of pre-commitment is to prevent rationalization in the moment.
- **Pause rule:** any "Pause if" trigger fires → weight set to 0% for review; bot stays in registry; resume requires explicit operator action.
- **Decision date:** **2026-06-12** (T+30 calendar days). First scheduled checkpoint at which retirement criteria are evaluated.
- **Companion truth surfaces:** `eta_engine/docs/DIAMOND_PROTECTION_2026_05_12.md`, `var/eta_engine/decisions/diamond_set_2026_05_12.md`, `eta_engine/scripts/diamond_falsification_watchdog.py` (early-warning buffer monitor).

---

## Roll-up table

| bot_id | symbol | weight | capital | lifetime paper P&L | sessions | 30d retire floor |
|---|---|---:|---:|---:|---:|---:|
| `mnq_futures_sage` | MNQ | 59.6% | $59,591 | +$5,552.52 | 14 | -$5,000 |
| `cl_macro` | CL | 13.4% | $13,392 | +$1,247.83 | 7 | -$1,000 |
| `cl_momentum` | CL | 11.4% | $11,354 | +$1,057.97 | 13 | -$1,500 |
| `mgc_sweep_reclaim` | MGC | 5.5% | $5,543 | +$516.51 | 13 | -$600 |
| `mcl_sweep_reclaim` | MCL | 5.2% | $5,197 | +$484.28 | 13 | -$1,500 |
| `eur_sweep_reclaim` | 6E | 2.5% | $2,505 | +$233.45 | 13 | -$300 |
| `gc_momentum` | GC | 1.5% | $1,525 | +$142.05 | 7 | -$200 |
| `zn_range` | ZN | 0.9% | $892 | +$83.11 | <7 | -$150 *(default)* |

---

### `mnq_futures_sage`
- **Promotion thesis**: Sage-gated CORB on MNQ 5m — 1,156 trade walk-forward backtest produced Sharpe 0.85, expectancy +0.064 R, WR 44.3%, profit-factor 1.115 over 119.8 coverage days; bot passed all gates as a Tier-1 1yr sub-kind dispatch survivor and is now the GOD-tier diamond with the largest paper-equity contribution (`reports/lab_reports/mnq_futures_sage/mnq_orb_sage_v1_lab_report.json`; `reports/lab_reports/PROMOTION_RECOMMENDATIONS_2026-05-04.md` — passed bot in `_fleet_sweep.json`).
- **Retire if** (any one trips → bot is retired, weight → 0):
  - Realized P&L ≤ -$5,000 over the next 30 calendar days (≈ -90% of lifetime gain) OR ≤ -45% drawdown from peak paper equity
  - Sharpe (last 30 sessions) drops below 0.30 (baseline 0.85 → trip = -65%)
  - Win rate (last 30 sessions) drops below 25% (baseline 44.3%)
  - Consecutive losing sessions ≥ 5
  - Max consecutive realized loss exceeds $2,500 (≈ -2σ of historical session P&L given $397/session mean)
- **Pause if** (intermediate trigger):
  - Sharpe (last 14 sessions) drops below 0.50 OR
  - 2 consecutive losing weeks OR
  - 30-day P&L < -$2,500 (half the retire floor — review window)
- **Decision date**: 2026-06-12.

### `cl_macro`
- **Promotion thesis**: Oil-macro fade (2× ATR spike with 0.5× ATR fade trigger) on CL during 2026 tariff/Mideast headline regime — +$1,247.83 paper P&L across 7 sessions; mechanic depends on >2σ ATR "panic spike" days remaining frequent (`var/eta_engine/decisions/diamond_set_2026_05_12.md` §8; `eta_engine/strategies/oil_macro_strategy.py` session-gated entries 12-16 UTC / 23-03 UTC).
- **Retire if**:
  - Realized P&L ≤ -$1,000 over the next 30 calendar days (≈ -80% of lifetime gain) OR ≤ -30% drawdown from peak paper equity
  - Sharpe (last 30 sessions) drops below 0.50 (no specific backtest Sharpe — uses default-of-default floor per `diamond_falsification_watchdog.py` cross-check)
  - Win rate (last 30 sessions) drops below 35%
  - Consecutive losing trades ≥ 3 (regime-decay tripwire — operator pre-commit)
  - "Panic spike" days (>2σ ATR move) drop below 4 per calendar month → mechanic-substrate failure
- **Pause if**:
  - Sharpe (last 14 sessions) drops below 0.80 OR
  - 2 consecutive losing weeks OR
  - <2 panic-spike trade days in the most recent 14-day window
- **Decision date**: 2026-06-12.

### `cl_momentum`
- **Promotion thesis**: ROC + ADX + MA-alignment trend-thrust on CL 1h — +$2,206 paper P&L across 13 sessions while the 2026 oil regime sustained ADX > 25; mechanic uses 2.0× ATR stops, 2.5 RR target, 0.5 ROC z-threshold (`var/eta_engine/decisions/diamond_set_2026_05_12.md` §3; `eta_engine/strategies/commodity_momentum_strategy.py:MomentumConfig`). Note: ledger shows +$1,058 in the recent allocation snapshot vs. +$2,206 lifetime in the diamond memo — falsification thresholds are anchored to the lifetime baseline.
- **Retire if**:
  - Realized P&L ≤ -$1,500 over the next 30 calendar days (≈ -68% of lifetime gain) OR ≤ -25% drawdown from peak paper equity
  - Single-session realized loss > $1,000 (volatility-spike kill — fires immediately, not 30d aggregate)
  - 3-month rolling ADX < 20 across all CL sessions (no trend regime → no momentum substrate)
  - Win rate (last 30 sessions) drops below 35%
  - Consecutive losing sessions ≥ 4
- **Pause if**:
  - Sharpe (last 14 sessions) drops below 0.50 OR
  - 2 consecutive losing weeks OR
  - 1-month ADX average < 22 (early-warning regime weakening)
- **Decision date**: 2026-06-12.

### `mgc_sweep_reclaim`
- **Promotion thesis**: Sweep_reclaim on MGC 1h — strict-gate v2 sweep produced Sharpe 1.72 / expR_net +0.124 across n=7 trades; +$853 paper P&L across 13 sessions; mechanic fades false liquidity sweeps that reclaim within N bars (`eta_engine/reports/strict_gate_mgc_mcl.json`; `var/eta_engine/decisions/diamond_set_2026_05_12.md` §5). Note: the more recent v2 run (`strict_gate_mgc_v2.json`) at n=15 showed Sharpe -3.89 — small-sample fragility is acknowledged; tighter retirement window reflects this.
- **Retire if**:
  - Realized P&L ≤ -$600 over the next 30 calendar days (≈ -70% of lifetime gain) OR ≤ -30% drawdown from peak paper equity
  - Sharpe (last 30 sessions) drops below 0.40 (baseline 1.72 strict-gate → trip at -77%)
  - Win rate (last 30 sessions) drops below 30%
  - n_trades < 3 in any 30-day window (signal-cadence cliff)
  - Consecutive losing sessions ≥ 5
- **Pause if**:
  - Sharpe (last 14 sessions) drops below 0.80 OR
  - 2 consecutive losing weeks OR
  - n_trades < 2 in any 14-day window
- **Decision date**: 2026-06-12.

### `mcl_sweep_reclaim`
- **Promotion thesis**: Sweep_reclaim on MCL 1h — strict-gate sweep produced Sharpe 2.00 / expR_net +0.111 across n=16 trades with the "split" diagnostic flag green; +$2,197 paper P&L across 13 sessions; mechanic identical to mgc_sweep_reclaim but on Micro Crude (`eta_engine/reports/strict_gate_mgc_mcl.json`; `var/eta_engine/decisions/diamond_set_2026_05_12.md` §4).
- **Retire if**:
  - Realized P&L ≤ -$1,500 over the next 30 calendar days (≈ -68% of lifetime gain) OR ≤ -25% drawdown from peak paper equity
  - Sharpe (last 30 sessions) drops below 0.50 (baseline 2.00 → trip at -75%)
  - Win rate (last 30 sessions) drops below 35% (reclaim mechanic requires false-sweep regime; trending oil kills the substrate)
  - Consecutive losing sessions ≥ 4
  - Max consecutive realized loss exceeds $850 (≈ -2σ given $169/session mean)
- **Pause if**:
  - Sharpe (last 14 sessions) drops below 0.80 OR
  - 2 consecutive losing weeks OR
  - Combined CL-equivalent open exposure exceeds 2 contracts (group-limit breach is a structural pause)
- **Decision date**: 2026-06-12.

### `eur_sweep_reclaim`
- **Promotion thesis**: Sweep_reclaim on 6E 1h — +$417 paper P&L across 13 sessions and explicitly flagged FRAGILE in the operator decision memo; kept as a low-correlation portfolio diversifier rather than a high-conviction edge (`var/eta_engine/decisions/diamond_set_2026_05_12.md` §6). Lab evidence is thin: the 2026-05-04 `_fleet_sweep.json` failed this bot with "bar file missing" (`reports/lab_reports/eur_sweep_reclaim/eur_sweep_reclaim_v1_lab_report.json` — n=0); promotion rests on the live paper-soak ledger, not a sweep number.
- **Retire if** (TIGHT — FRAGILE status):
  - Realized P&L ≤ -$300 over the next 30 calendar days (≈ -72% of lifetime gain) OR ≤ -20% drawdown from peak paper equity
  - Sharpe (last 30 sessions) drops below 0.50 (default — no sweep-baseline available)
  - Win rate (last 30 sessions) drops below 50% (high WR floor matches the bot's role as a low-edge diversifier)
  - Any rolling 14-day window with negative P&L (no recovery cycle — pre-commit)
  - Consecutive losing trades ≥ 3
- **Pause if**:
  - Sharpe (last 14 sessions) drops below 0.80 OR
  - 1 losing week with P&L < -$150 OR
  - n_trades < 4 in any 30-day window (cadence cliff — 6E signal noise floor)
- **Decision date**: 2026-06-12. Reviewed weekly given FRAGILE status — operator may pull early on any single-week breach.

### `gc_momentum`
- **Promotion thesis**: GC 1h momentum (ROC + ADX + MA thrust) — DXY-gold-inverse sweep matched the mechanic with Sharpe 1.185 (n=555) and 1.394 (n=862) at lb=20/trend=50 and lb=10/trend=30 settings (`reports/lab_reports/new_strategies_sweep.json` rows 2–46); +$142 paper P&L across only 7 sessions — explicitly FRAGILE in the operator memo (`var/eta_engine/decisions/diamond_set_2026_05_12.md` §7). The smallest paper buffer in the diamond set.
- **Retire if** (TIGHTEST — only $142 lifetime buffer):
  - Realized P&L ≤ -$200 over the next 30 calendar days OR ≤ -15% drawdown from peak paper equity
  - Sharpe (last 30 sessions) drops below 0.50 (baseline 1.185 sweep → trip at -58%)
  - Win rate (last 30 sessions) drops below 32% (sweep baseline 36.9–37.6%)
  - Consecutive losing trades ≥ 5 (operational kill switch per memo §7)
  - MC verdict ≠ ROBUST after the second monthly evaluation
- **Pause if**:
  - Sharpe (last 14 sessions) drops below 0.80 OR
  - 2 consecutive losing weeks OR
  - 3 consecutive losing trades (early-warning of the kill-switch trigger)
- **Decision date**: 2026-06-12. Reviewed weekly. **Operator may pull early on the 5-loss-streak trigger without waiting for the 30-day window.**

### `zn_range`
- **Promotion thesis**: Range mean-reversion on 10Y Treasury 1h using BB(20, 2.5) + RSI(14) with 1.0× ATR stops, 2.5 RR; bot is registered as `strategy_kind="fx_range"` with `promotion_status="research_candidate"` (`eta_engine/strategies/per_bot_registry.py` lines 3233–3255). The closest matched lab evidence is the `treasury_safe_haven` mechanic on ZN (a different strategy kind on the same instrument) which passed with Sharpe 1.366 (n=144) and 1.421 (n=146) at lb=5–10/spike=10–15% (`reports/lab_reports/macro_strategies_sweep.json` rows 169–211). Lifetime paper P&L +$83 across <7 sessions — **lowest-conviction member of the active fleet**.
- **Retire if** (DEFAULT thresholds — not directly evidence-based for the actual `fx_range` kind; the cross-mechanic ZN baseline is used as a floor proxy):
  - Realized P&L ≤ -$150 over the next 30 calendar days OR ≤ -20% drawdown from peak paper equity
  - Sharpe (last 30 sessions) drops below 0.50 (default floor — no sweep on the registered fx_range mechanic)
  - Win rate (last 30 sessions) drops below 35% (treasury_safe_haven baseline was 37.5–37.7%; -2.5pp tolerance)
  - Consecutive losing sessions ≥ 4
  - n_trades < 2 in any 30-day window (research_candidate cadence floor — if signals don't materialize, the registration is wrong)
- **Pause if**:
  - Sharpe (last 14 sessions) drops below 0.80 OR
  - 2 consecutive losing weeks OR
  - First losing month (any 30-day window negative) — pre-commit elevated scrutiny for `research_candidate` promotions
- **Decision date**: 2026-06-12. Reviewed weekly. **DEFAULT thresholds are explicitly NOT evidence-based for the registered mechanic.** If the bot survives the 30-day window, a dedicated `fx_range` sweep MUST be run before the 2026-08-12 90-day re-review to replace these placeholders.

---

## Why these criteria exist

Without pre-committed retirement rules, paper edges become live drawdowns because there is no rule that pulls a diamond off the line — every losing streak gets rationalized as a "rough patch" and every fading edge gets attributed to "regime noise" until the cumulative loss is larger than the original buffer. Pre-commitment moves the retirement decision from the heat of a drawdown (where loss-aversion biases keep losers running) to the cold of a backtest review (where the operator has no skin in any particular outcome). The numbers above are the operator's binding promise to themselves at the moment of clearest thinking; if any one fires, the bot is retired within 24 hours of detection — no debate, no re-litigation, no "let it run one more week."
