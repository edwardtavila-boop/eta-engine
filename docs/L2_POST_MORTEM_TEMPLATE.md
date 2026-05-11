# L2 Strategy Post-Mortem

**Strategy:** `<bot_id>` (e.g. `mnq_book_imbalance_shadow`)
**Date:** YYYY-MM-DD
**Triggering event:** Retirement / Drawdown / Live incident / Falsification

---

## 1. What happened

(One paragraph factual narrative — what happened, when, in what session/regime.)

## 2. Final metrics

| Metric | Value | Threshold | Triggered? |
|---|---|---|---|
| Total trades | `<N>` | n/a | n/a |
| Cumulative net P&L | `$<X>` | n/a | n/a |
| OOS sharpe (latest 30d) | `<X>` | `< 0 → retire` | yes/no |
| 14-day rolling sharpe (worst) | `<X>` | `< -0.5 → retire` | yes/no |
| Brier score on confidence | `<X>` | `> 0.30 (n>=100) → retire` | yes/no |
| Sharpe CI 95% upper bound | `<X>` | `< 0 → retire` | yes/no |
| Max drawdown | `<X%>` | n/a | n/a |
| Worst single-session loss | `$<X>` | daily loss limit | yes/no |

## 3. Red Team dissent retrospective

Quote the Red Team's attacks **verbatim** from the original Firm review:

> "<paste attack 1>"

**Did it happen?** YES / NO / PARTIAL — explain.

> "<paste attack 2>"

**Did it happen?** YES / NO / PARTIAL — explain.

(Repeat for each attack. The Red Team gets a calibration score based on how many predictions verified.)

## 4. What the agents missed

What did the strategy do that NO Firm agent predicted?

- `<…>`

## 5. Single-trade autopsies (worst 3 trades)

For each:
- Trade ID / signal_id
- Setup (which gate fired, confidence, regime)
- Entry / stop / target prices
- Exit reason + actual fill price
- Realized slip vs predicted
- Was the strategy state correct at entry? (kill-switches, gap-reset, etc.)
- Root cause: edge mispriced / fill realism / regime fragility / operational

## 6. What the data says, separately from the post-mortem narrative

- Win rate by session bucket (RTH_OPEN/MID/CLOSE/ETH): `<…>`
- Win rate by regime: `<…>`
- Win rate by confidence decile: `<…>`
- Brier score evolution over time: `<…>`
- Slip distribution evolution: `<…>`

## 7. Lessons captured

For the next L2 strategy:
- `<…>`

For the harness / sweep / evaluator:
- `<…>`

For the order router:
- `<…>`

For the Firm process (if any agent missed something material):
- `<…>`

## 8. Cleanup actions

- [ ] Set `promotion_status` to `deactivated` in `l2_strategy_registry.py`
- [ ] Document `deactivated_on` + `deactivated_reason` in the registry entry
- [ ] Add cleanup to `ASSIGNMENTS` removing the bot from active fleet
- [ ] Archive all Firm artifacts to `var/eta_engine/firm_archive/<bot_id>_<YYYY-MM-DD>/`
- [ ] Manually close any open positions (operator action)
- [ ] Update `MEMORY.md` with the lesson learned

## 9. Sign-off

- Operator: _____________________  Date: __________
- Next review: __________ (typically 7 days post-retirement to confirm cleanup)
