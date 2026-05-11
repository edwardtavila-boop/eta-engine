# L2 System — June/July 2026 Roadmap

**Premise:** The L2 supercharge stack is engineering-complete as of May 2026.
274 supercharge+hardening tests pass; 6800+ broader tests pass; every layer of
measurement, falsification, anomaly detection, drift detection, and operator
review is built. The system needs only:

1. Operator-side activations (subscriptions, supervisor wiring, cron registration)
2. ~14 days of clean shadow capture
3. Per-strategy decision memos at each promotion gate
4. First live trade decision

The roadmap below is **forward-looking** — six initiatives that each require
significantly more infrastructure (FPGA hardware, ML pipelines, options data,
prime brokerage, RL simulators). Most are 4-12 weeks of focused work each.
None are required for the current L2 system to operate. They represent the
next tier of capability after the current stack proves itself with live data.

---

## Tier 1 (June priorities)

### G1. Production-grade ML slip predictor — `4 weeks`

**Current state:** `l2_slippage_predictor.py` is a bin-and-average regression.
Predicts slip as a function of (regime, session_bucket, size_bucket, vol_bucket).

**June upgrade:** Replace with gradient-boosting regressor (XGBoost or LightGBM)
trained on the full fill log. Features:
- Realized volatility (1min/5min/1hr rolling)
- Time-of-day continuous (not just bucket)
- Spread regime + spread vs median ratio
- Order size relative to top-N book qty
- Recent trade-print imbalance
- Days-to-expiry
- News-event proximity (minutes until/since FOMC/NFP/CPI)

**Prerequisites:**
- Need ≥1000 real broker fills to train (≥4 weeks of paper-soak with the wired supervisor)
- pip-install xgboost/lightgbm in VPS Python env
- A held-out test set policy (last 20% of fills, rolling)

**Success criteria:**
- Cross-validated R² > 0.30 on slip prediction
- p90 prediction error < 1 tick on out-of-sample
- Operator-callable via existing `predict_slip()` API (backwards-compatible)

**Falsification:** If R² < 0.10 with 2000 fills, abandon ML approach and stay with bin-and-average.

---

### G2. Real-time risk engine (VaR + Expected Shortfall) — `4 weeks`

**Current state:** Risk is per-trade (stop-loss), per-strategy (max_qty), and per-portfolio (`l2_portfolio_limits.py`). Static thresholds.

**June upgrade:** Live portfolio risk engine that:
- Maintains rolling covariance matrix across symbols (90-day exponential decay)
- Computes Value-at-Risk (95th + 99th percentile) on the live portfolio
- Computes Expected Shortfall (CVaR) — expected loss in tail scenarios
- Updates every 30s during RTH
- Feeds `trading_gate` with a "risk_budget_remaining" signal — strategies refuse new entries when VaR ≥ daily limit

**Prerequisites:**
- Need ≥30 days of clean depth + fill data for covariance estimation
- pip-install scipy.stats (or implement Cornish-Fisher VaR pure-Python)
- New cron entry `ETA-L2-VarEngine` running every 30s on VPS

**Success criteria:**
- Realized daily P&L breaches VaR-99 in ≤1% of sessions over 90 days
- ES estimate predicts realized tail-loss within 30% on the worst 5% of days
- Strategy gate latency adds <5ms to entry path

**Falsification:** If VaR-99 is breached >5% of days, the covariance estimate is wrong or the strategies have hidden leverage — investigate before scaling.

---

### G3. Reinforcement-learning policy (offline phase 1) — `6 weeks`

**Current state:** Strategy parameters tuned via grid sweep + deflated sharpe selection. CPCV validates against overfitting.

**June+July upgrade:** Train an offline RL policy on the supercharge harness as the simulator. Phase 1 — offline-only, no live trading:
- State: depth-snapshot features + recent tick stats + portfolio state
- Action: {LONG, SHORT, NO_TRADE, EXIT}
- Reward: per-trade pnl_dollars_net
- Algorithm: PPO or SAC over the simulator built from real captured ticks
- Train on 30-90 days of replay; validate via CPCV folds

**Prerequisites:**
- Need ≥30 days clean depth + tick data
- pip-install torch + ray/rllib (or stable-baselines3 for simpler PPO)
- VPS GPU optional but accelerates training 5-10×
- Strategy state captured in the same format the live supervisor uses

**Success criteria (June endpoint):**
- RL policy converges in CPCV with sharpe ≥ best individual L2 strategy
- Policy's actions reproducible (deterministic seed)
- Operator-readable policy summary (which states trigger which actions)

**Success criteria (July endpoint):**
- RL policy promoted to shadow status in `l2_strategy_registry`
- Shadow-soaked alongside the 4 hand-coded strategies
- Decision memo signed by operator

**Falsification:** If RL policy fails to outperform best hand-coded strategy in CPCV after 3 hyperparameter sweeps, abandon — the structural priors in the hand-coded strategies are doing most of the work.

---

## Tier 2 (July priorities)

### G4. Options Greeks layer — `6 weeks`

**Current state:** Futures-only. No options data feed.

**July upgrade:** Add options chain consumer + Greeks calculator:
- Subscribe to MNQ options chain (OPRA subscription, $1.50/mo)
- Implement Black-Scholes-Merton Greeks (delta, gamma, vega, theta) — pure Python (no scipy)
- Wire into a position-level Greeks aggregator
- Enable delta-hedge calculations on futures position via 1-touch ATM options

**Prerequisites:**
- IBKR Pro OPRA subscription ($1.50/mo)
- Operator decision: are we hedging futures with options, or trading options directly?
  - Hedging-only is simpler (smaller scope, lower risk)
  - Direct trading needs whole new strategy family

**Success criteria:**
- Real-time Greeks on any open futures position via synthetic option position
- Hedge-ratio calculator that gives operator a "buy N ATM puts to cap downside" recommendation
- Backtest shows hedge reduces max drawdown by ≥30% with cost ≤20% of edge

**Falsification:** If hedge cost > 50% of edge across 6 weeks of paper-soak, abandon — the L2 stack already has portfolio-level circuit breakers; options hedge is redundant overhead.

---

### G5. Multi-account routing — `4 weeks`

**Current state:** Single-broker (IBKR Pro paper → live). Tradovate dormant.
Crypto routes via Alpaca. Each strategy hardcoded to one venue.

**July upgrade:** Generalize the order router to choose venues at runtime based on:
- Subscription state (which venue has the symbol active?)
- Latency (which venue's gateway is healthy?)
- Margin requirements (some brokers reject MNQ overnight; route to one that doesn't)
- Cost (commission per RT differs by broker)

**Prerequisites:**
- Tradovate or other secondary broker reactivated
- Per-broker auth files + connection pools
- Per-broker fill schema normalization layer

**Success criteria:**
- Operator can `--venue alpaca|ibkr|tradovate` at runtime per strategy
- Failover: if primary venue is down, automatic fallback to secondary
- No single venue's outage stops trading

**Falsification:** If commission/slip cost on the secondary broker exceeds IBKR by >30%, the routing layer is overhead — operator should single-broker until that changes.

---

### G6. Sub-100μs HFT infrastructure — `8+ weeks (not a June/July goal)`

**Current state:** Python supervisor with ~10ms entry latency under typical load.

**This is NOT a June/July goal.** Building sub-100μs latency requires:
- Co-located server (~$1500/mo at CME Equinix)
- FPGA-based market data + order placement (custom hardware, $5k-50k upfront)
- C++ or Rust execution stack (Python is too slow for the sub-ms path)
- Direct exchange membership or sponsored access

The L2 stack's strategies operate on 5-15s decision horizons. HFT-tier latency
buys nothing for them. The operator should NOT spend Q2 building this. If
research after Q2 shows microprice-style edges that decay sub-100ms, revisit.

Document this as **deferred to 2027 unless evidence emerges that the current
strategies are leaving sub-100ms edge on the table**.

---

## Tier 3 (Q3 2026, post-paper-soak)

### G7. Cross-venue arbitrage execution

Current: `l2_cross_broker_arb.py` is MONITOR-ONLY. Q3 upgrade adds execution:
- Confirmed real-time data on both legs
- Atomic order placement (cancel-on-fail second leg)
- Slip + commission accounting per leg
- Edge filter: only execute when bps_diff > 2 * (slip_a + slip_b + commission_a + commission_b)

### G8. Survivorship-bias-free universe expansion

Current capture set is curated. Q3 upgrade: probe ALL CME futures (including
the ones never traded), measure their L2 dynamics, evaluate book_imbalance edge
across the full universe. Catches edges in less-popular contracts where
crowding hasn't competed away the alpha.

---

## What ISN'T in this roadmap (deliberately)

- Crypto strategies — Alpaca crypto fleet already exists, separate team
- Equity strategies — covered by per_bot_registry legacy fleet
- Forex — IBKR pairs but operator has no edge there
- Earnings season equity scalping — wrong instrument class
- Macroeconomic backtesting — too noisy to be tradable

---

## Operator action items (current — before any roadmap work)

These gate all forward progress. Roadmap items G1-G5 require live fill data,
which requires:

1. **IBKR subscription Continue + paper-inheritance toggle** (5 min)
2. **VPS `register_l2_cron_tasks.ps1 -StartNow`** (1 min)
3. **Apply supervisor wiring diff** from `docs/L2_SUPERVISOR_WIRING.md` (15 min)
4. **Populate news_blackout calendar** — DONE in May 2026 commit; see `logs/eta_engine/l2_news_events.jsonl`
5. **Persist supervisor open_positions** to the file `l2_reconciliation` reads — operator integrates `l2_supervisor_state_persister.py` into supervisor heartbeat (15 min)
6. **14 days of clean shadow capture** — pure time
7. **Per-strategy decision memo signing** at each promotion gate
8. **First live trade decision**

After (1)-(8), the system runs autonomously and the roadmap above starts to make sense.

---

## Resource budget for June/July roadmap

| Item | Capital | Time |
|---|---|---|
| G1 ML slip predictor | $0 (uses existing data) | 4 weeks |
| G2 Real-time risk engine | $0 | 4 weeks |
| G3 RL policy offline | $0 (CPU) or ~$200 (1 mo GPU instance) | 6 weeks |
| G4 Options layer | $1.50/mo OPRA sub | 6 weeks |
| G5 Multi-broker | ~$59 Tradovate DORMANT eval (already done; no reactivation without operator code+docs approval) | 4 weeks |
| **Total** | **~$200-400 one-time + $1.50/mo** | **~6 weeks elapsed if parallelized** |

The dollar cost is trivial. The time is the constraint — each item needs
focused operator attention to design, deploy, monitor.

---

## Decision criteria for starting each item

**G1 (ML slip):** Wait until ≥1000 broker fills exist in `broker_fills.jsonl`.
**G2 (VaR):** Wait until ≥30 days of clean covariance-estimation data.
**G3 (RL):** Wait until G2 is online (RL state depends on real-time risk).
**G4 (options):** Independent — can start anytime after operator decides
                    hedging vs direct trading.
**G5 (multi-broker):** Independent — can start anytime, ideally after G4 (which
                         may benefit from broker choice).
**G6 (HFT):** Deferred to 2027 unless sub-100ms edge evidence emerges.

---

**Signature:**
- Roadmap author: Edward Avila + Claude (Anthropic supercharge agent)
- Date: 2026-05-11
- Next review: 2026-06-15 (after first 2 weeks of June progress)
