# Kill criteria — MBT/MET research bots (PRE-REGISTERED)

> **Purpose**: lock in objective stop conditions BEFORE any paper-soak begins.
> Without a pre-registered kill criterion, "paper soak" becomes a sunk-cost
> ratchet — every loss day produces "let's give it another week" and the
> strategy never gets retired. This document fixes that.
>
> **Required signature**: operator must sign + date below before promoting
> any of the 3 bots from `research_candidate` to `paper_soak_active`.

## Bots covered

- `mbt_funding_basis` — MBT short-side z-score fade (basis-decay rationale,
  log-return proxy in production today)
- `mbt_overnight_gap` — MBT mean-reversion gap fade at NY open
- `met_rth_orb` — MET 5m Opening Range Breakout

## Prior elite review findings (informing these gates)

The 5-agent review (`quant-researcher`, `market-microstructure`,
`risk-execution`, `devils-advocate`, `Explore`) on 2026-05-07 produced these
calibrated priors:

- Probability ANY of the 3 has a real, persistent, costed-in-slippage live
  edge: **~8%**
- Probability all 3 are noise: **~78%**
- Sample-size floor: **<30 OOS trades = noise** at typical Sharpe scales
- Friction floor: **MET round-trip = $2.70 per contract** (commission +
  half-spread); MET 2R win = $3.30 net = needs **>63% win rate** to be
  profitable
- Prior MBT/MET sweep_reclaim attempt failed: **Sharpe -0.71 / -0.47**

These priors are not optional — they are the reason these gates exist.

## Gate 1 — Walk-forward validation (BEFORE paper-soak)

For EACH bot, lab harness must produce a walk-forward report meeting ALL of:

| Metric | Threshold | Notes |
|---|---|---|
| OOS trade count | **≥ 60** | Below 60 trades, Sharpe SE is wider than the threshold itself; result is noise. |
| Aggregate OOS Sharpe (Deflated, Lopez-de-Prado) | **≥ 1.0** | Use Deflated Sharpe — accounts for the 3-strategy + parameter-search multiple-testing horizon. Naive Sharpe ≥ 1.0 alone is NOT sufficient. |
| OOS max drawdown | **≤ 12%** | Hard ceiling. Folds that breach are individual fails. |
| Per-fold pass rate | **≥ 50%** | Of N walk-forward folds, at least N/2 must individually meet trades + Sharpe + DD. Single hot fold is insufficient. |
| Bootstrap 5th-pct equity-curve max DD | **≥ -8%** | Block bootstrap (size 5–10 trades) on OOS pnl_R series, 5000 reps. |
| Friction sanity | Net expectancy after **2.5 ticks slippage + $2.50 RT commission** must be ≥ 0 | Runs the harness with `realistic_fill_sim` ON, not pristine bar-close fills. |

**Failure of any single gate = HALT promotion**. Either retire the strategy
or fix the issue and re-run from scratch (no parameter tuning to "make it
pass" — that's overfitting).

## Gate 2 — Paper-soak entry conditions

ONLY after Gate 1 passes:

- Run `register_paper_soak_task.ps1` for the bot.
- Soak window: **30 calendar days**, no extension.
- During soak: `_MAX_QTY_PER_ORDER` clamped to **1 contract** (half normal).
- Cross-bot fleet cap: **3 contracts net per root** (already enforced via
  `cross_bot_position_tracker`).

## Gate 3 — Paper-soak exit conditions (KILL CRITERIA)

After 30 days OR earlier-trigger conditions below, the bot is RETIRED if
ANY of:

| Condition | Threshold | Action |
|---|---|---|
| Total paper-soak trades | **< 25** | Retire — insufficient sample, do NOT extend the soak. |
| Realized Sharpe over the soak window | **< 0.5** | Retire. |
| Max drawdown within window | **> 8%** | Retire. |
| Realized expR (R-multiple expectation) | **< +0.05** | Retire. |
| Win rate (for MET ORB only) | **< 50%** | Retire — friction floor demands ≥63% live; <50% over 25 trades is fatal. |
| Single-day loss | **> $250** | Halt for next session, escalate to operator. Two such days = retire. |
| Any divergence between strategy stat tracker and broker truth | Detected by reconciler | Halt + investigate. |

## Gate 4 — Live-cutover conditions (after successful soak)

After 30-day soak passes Gate 3:

- 60-day SECOND soak at 1 contract (no scale-up).
- Realized Sharpe ≥ 1.0 over the 60-day window.
- Cross-bot net P&L over 60 days ≥ +$300 (cushion above noise).
- Operator manual sign-off on the consolidated 90-day record.

NO live cutover before all four gates have closed in sequence.

## Specific kill triggers per bot (additional)

### `mbt_funding_basis`

- **HALT NOW** if no real `basis_provider` is wired by Day 7 of soak (today
  using `LogReturnFallbackProvider` — strategy is mislabeled). The honest
  rename is `mbt_zfade`.
- If the basis_provider is wired and the strategy still fires <8 trades in
  the 30-day soak: retire (sample too thin to evaluate even with real basis).

### `mbt_overnight_gap`

- **HALT NOW** if a contrived test confirms the `_last_rth_close` anchor
  selects ETH (overnight) bars. (The 2026-05-07 fix walks hist backwards
  for a prior-day RTH bar — verify with a real bar feed during soak.)
- If the bar-direction confirmation filter (lines 266–269 of the strategy)
  reduces trade count by >40% relative to gap-detection count: retire,
  the filter is selecting the failure mode.

### `met_rth_orb`

- The 60-day yfinance smoke walk-forward (2026-05-07) reported
  Sharpe=5.93 on 25 trades. Per quant-researcher: "Sharpe SE on 25 trades
  is roughly ±0.5 — a measured Sharpe of 5.93 has a 95% CI that
  comfortably includes 4.5 to 7.5." This is a small-sample lottery
  result, NOT evidence of edge. Walk-forward must be re-run on 540-day
  IBKR data and DEFLATED across 3 strategies + parameter search before
  this number is admissible.
- If 540-day walk-forward Sharpe drops below 1.0 (without parameter
  re-tuning): retire. The 60-day result was noise.
- If realistic_fill_sim shows >40% drawdown vs pristine-fill backtest
  on the same window: retire — the strategy lives or dies on the
  spread, and on MET the spread is ~50% of the 2R reward.

## Operator commitment

By signing below, the operator acknowledges:

- They will NOT extend the 30-day soak window if the bot is below
  trade-count threshold ("more time will surface the edge" is selection
  bias).
- They will NOT silently re-tune parameters mid-soak ("just one knob"
  is overfitting).
- They will NOT promote to live without the second 60-day soak gate.
- They WILL retire any bot that hits a kill condition, even if it's
  "close" — the cost of false-positive shipping > cost of false-negative
  retirement at this stage.

| Signature | Date |
|---|---|
| (operator name) | (YYYY-MM-DD) |

---

*This document was drafted 2026-05-07 by Claude Code based on the
5-agent elite review. It is a forcing function, not a suggestion. The
8% prior was derived from the agents' adversarial analysis; the 78%
"all noise" probability requires a strict gate to avoid throwing good
money / time after bad.*
