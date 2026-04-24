# MNQ Live Operations Protocol (LIVE-OPS v1)

**Status:** Active  |  **Owner:** Edward Avila  |  **Generated:** 2026-04-24
**Scope:** MNQ futures, 1-contract live-tiny. **IBKR primary + Tastytrade fallback**
(active 2026-04-24). Tradovate path described below is DORMANT while the
account is funding-blocked — substitute IBKR auth for T5 until the Tradovate
dormancy flag is cleared.
**Supersedes:** the portfolio-level guidance in `canonical_v1_verdict_full.md`
for anything touching live execution.

> One page. If you cannot see it on your phone in 30 seconds, it does not
> count as a standardized process. Every live session starts here.

---

## 1. The only reason we are live

To **prove or kill** the MNQ standalone edge. Paper says +0.212R expectancy on
n=63 trades over 4 weeks; bootstrap CI95 spans zero (−0.087 to +0.512). We do
not know yet whether the edge is real — live sample closes that gap. Every
decision below exists to protect the sample.

---

## 2. Trigger rules — WHAT MUST BE TRUE BEFORE I CLICK START

ALL of these, every session, no exceptions:

| # | Check | Command | Pass criterion |
|:-:|---|---|---|
| T1 | Preflight all required gates | `python -m apex_predator.scripts.live_tiny_preflight_dryrun` | 14/14 required PASS |
| T2 | Clock drift < 3s | grep `clock_drift` in T1 output | PASS |
| T3 | Paper baseline current | `ls -lt docs/paper_run_report_mnq_only_v2.json` | ≤ 7 days old |
| T4 | Journal directory writable | `touch docs/journals/live/.ok && rm $_` | no error |
| T5 | Tradovate demo auth | Phase 2 smoke block from runbook | access_token obtained |
| T6 | Market session | **RTH ONLY** 09:30–16:00 ET (see §13) | inside window |
| T7 | Kill-switch yaml loads | `python -c "import yaml; yaml.safe_load(open('apex_predator/configs/kill_switch.yaml'))"` | no error |
| T8 | Last reconcile GREEN | `python apex_predator/scripts/_trade_journal_reconcile.py --hours 24` | exit 0 |

If ANY is RED → DO NOT START. Fix or abort the session.

---

## 3. Block rules — WHEN TO HALT IN-SESSION (automated + manual)

### Automated kill-switch (runs continuously)
Trips `FLATTEN_ALL + CRITICAL` when any of:
- Realized portfolio DD ≥ 50% of session cap
- 5+ consecutive losers
- Any Tradovate HTTP 5xx for 3 minutes
- Telegram heartbeat silent > 2 minutes
- Local equity diverges from venue equity by > $50

### Manual halt triggers (operator discretion)
- Live-vs-paper drift **RED** on any run of:
  `python apex_predator/scripts/live_vs_paper_drift.py --hours 24`
- Single trade fills > 2 ticks adverse slippage (watch the first 5 orders closely)
- Bracket order fails to register at venue after 2 attempts
- Any "UNKNOWN" verdict from journal reconcile

---

## 4. Size rules — WHAT QTY IS ALLOWED

**Phase 4 (first 30 days, the current phase):**
- Max **1 MNQ contract** per trade.
- Risk per trade **0.5%** of account (= $25 on $5,000 bucket).
- Max **4 trades/day** (not 6 like v1).
- Account cap: $5,000. NOT the Apex funded account yet.

**Phase 5 (weeks 2–4):** no change until n ≥ 60 live trades AND drift tracker
is GREEN 3 days running.

**Phase 6 (after n ≥ 120):** only advance to 2 contracts if:
- Live walk-forward STABLE (pass_rate ≥ 67% of folds at +0.05R gate)
- Live bootstrap CI95 EXCLUDES zero
- Zero RED drift days in the past 2 weeks

**NEVER** add a second bot in Phase 5–6 without running
`portfolio_correlation_audit.py` against the candidate. If daily-R rho > 0.4,
the candidate does not qualify as diversification — it qualifies as
duplicate risk.

---

## 5. Stop rules — HOW TO SHUT DOWN

### Graceful (preferred)
```bash
# in the live terminal:
Ctrl-C  →  wait for "runtime_stop" alert in docs/alerts_log.jsonl
```

### Hung supervisor (escalation)
```bash
sudo systemctl stop apex-live    # Linux
# or:
pkill -f run_apex_live
```

### Emergency: positions still open after supervisor gone
1. Open Tradovate UI.
2. Orders tab → **Cancel All Working**.
3. Positions tab → **Flatten All** (market).
4. Immediately: `python apex_predator/scripts/_trade_journal_reconcile.py --hours 1`.
5. Snapshot `docs/alerts_log.jsonl` → `docs/incidents/YYYYMMDD.jsonl`.
6. Append root-cause entry to `docs/kill_log.json`.

---

## 6. Failure definition — WHAT COUNTS AS A BAD SESSION

A session is a **failure** and must be reviewed before the next one if ANY:
- Realized session PnL < −$200 (4× 1R stop, unusual for a single session).
- Drift tracker exits RED.
- Kill-switch fired at CRITICAL.
- Venue-vs-journal discrepancy > 1% of account equity.
- More than 2 operator-manual intervention events during the session.

Reviews use the `firm-tracker` skill or a manual read of the day's
`alerts_log.jsonl` + journal.

---

## 7. Daily check-list (automated, runs 08:00 ET next day)

```bash
# Add to cron or run manually each morning:
python apex_predator/scripts/_trade_journal_reconcile.py --hours 24
python apex_predator/scripts/live_vs_paper_drift.py --hours 24 \
    --paper-baseline apex_predator/docs/paper_run_report_mnq_only_v2.json
python apex_predator/scripts/_kill_switch_drift.py --hours 24
python apex_predator/scripts/session_scorecard_mnq.py --mode live --hours 24 \
    --journal apex_predator/docs/journals/live/mnq_journal.jsonl \
    --paper-baseline apex_predator/docs/mnq_v2_trades.json
```

All three must exit 0. If any exits 1 (YELLOW), continue with caution. If any
exits 2 (RED), HALT next session until root cause fixed.

---

## 8. Weekly check-list (every Sunday)

- Re-run bootstrap-CI on accumulated live sample:
  `python apex_predator/scripts/bootstrap_ci_real_bars.py --iterations 10000 --weeks 1 --seed 11 --symbols mnq --label mnq_live_wk<N>`
- Re-run walk-forward:
  `python apex_predator/scripts/walk_forward_real_bars.py --weeks 4 --stride-weeks 1 --max-folds 6 --symbols mnq --label mnq_live_wk<N>`
- Update sample-size tracker:
  `python apex_predator/scripts/sample_size_calc.py --report docs/bootstrap_ci_mnq_live_wk<N>.json --label mnq_live_wk<N>`

---

## 9. Gauntlet to Phase 6 (the graduation gate)

Advance to 2 contracts ONLY after **ALL** six conditions (measured on live
sample, not paper):

1. n_trades ≥ 120
2. walk-forward pass_rate ≥ 67%  (STABLE verdict)
3. bootstrap CI95 EXCLUDES zero
4. no RED drift-tracker day in past 14 calendar days
5. no CRITICAL kill-switch event in past 14 days
6. realized expectancy ≥ +0.10R (half of paper figure — conservative buffer)

Missing any → STAY at 1 contract. Re-evaluate in 1 week.

---

## 10. Absolute NO-GO actions (never, regardless of context)

- Live trading without preflight GREEN.
- NQ, SOL, ETH, XRP, or crypto_seed bots live. These are paper-only.
- Weekend sessions while in Phase 4–5.
- Running multiple bot instances against the same account.
- Skipping the daily reconcile.
- Trusting a "probably fine" clock-drift FAIL.
- Scaling up without re-running the gauntlet.
- Advancing to a second bot before 2 months of clean MNQ-only soak.

---

## 11. What MNQ actually trades on (the 20/80)

Internally the bot scores 5 confluence axes (trend_bias, vol_regime, funding_skew,
onchain_delta, sentiment). **On MNQ, only three signals matter for trade triggering:**

| Axis / Signal | Component | Drives |
|---|---|:-:|
| **trend_bias** | sign of EMA9 − EMA21 on 5-min bars | 40.8% of score |
| **vol_regime** | ATR(14) in sweet-spot range 0.3–0.8× average | 25.4% of score |
| **ADX filter** (overlay) | ADX(14) ≥ 18 required to enter (chop suppression) | pass/fail gate |

`funding_skew` is 0 every bar (futures has no funding). `onchain_delta` and `sentiment`
are crypto-tuned and contribute noise from neutral placeholders — they do not materially
move the MNQ trigger.

**Operator rule of thumb:** MNQ trades when the 5-min EMA9 separates from EMA21 by enough,
in a normal-ATR regime, and the market is trending (ADX ≥ 18). If you want to know why a
trade fired, inspect those three numbers — they reconstruct ≥95% of the confluence score.

The scorer's max observed value on MNQ is 6.42; the 20× and 75× leverage rungs at 7.0 and
9.0 are permanently unreachable with this axis set, so **every MNQ trade is sized at
leverage=10 (REDUCE tier).** No "conviction bonus" exists for MNQ at this time.

Full audit: `docs/mnq_feature_contribution_audit.md`.

## 12. Execution-latency rules — fill-age gate + RTH-only

Latency stress shows the MNQ edge survives +1 bar of fill delay (10 min
signal-to-fill total) but degrades materially at +2 or more bars.

| Fill age (bars) | Expectancy vs baseline | Action                       |
|---             :|---                     |---                            |
| ≤ 1             | 0R (baseline)          | Trade proceeds normally       |
| 2               | +0.036R                | Log as **warning**, continue  |
| 3               | **−0.089R**            | Cancel pending, log `FILL_AGE_EXCEEDED` |
| 5+              | −0.053R                | Cancel pending + halt session |

**Operator rules:**

- Order supervisor MUST cancel any pending bracket that has not filled by
  the open of bar N+2 from the signal bar. Do not chase.
- Log every `FILL_AGE_EXCEEDED` event to `alerts_log.jsonl` (level=WARNING).
- `session_scorecard_mnq.py` reports YELLOW if `over_1_bar ≥ 3` trades in a
  session, RED if `over_2_bars ≥ 2`.
- Pre-flight network RTT check (Tradovate REST, 30 samples): p95 > 800 ms
  demotes T-check to YELLOW. p95 > 1500 ms = HARD ABORT.

Details: `docs/mnq_latency_stress.md`.

## 13. Session-hour rule — RTH ONLY until further notice

Paper breakdown by session window (n=63 trades, 4-week sample):

| Session            | n  | Expectancy | Win% |
|---                 |---:|---        :|---  :|
| **RTH (09-16 ET)** | 23 | **+0.581R**|65.2% |
| Late aft (16-18)   |  7 | +0.429R    |57.1% |
| **US evening**     | 21 | **−0.167R**|33.3% |
| Asia open (23-03)  |  6 | −0.167R    |33.3% |
| London (03-09)     |  6 | +0.250R    |50.0% |

**Rule:** Live sessions are restricted to **09:30–16:00 ET** (RTH). Any trade
outside that window violates T6 and the operator MUST abort.

**Why:** US evening is net-negative on the paper sample (likely thin-liquidity
slippage + indices chop in Asia-driven hours). Running it live adds expected bleed
without any measured upside. We are deliberately dropping 33% of the sample to
double the realized expectancy.

**This rule is reversible.** If RTH-only live shows drift below +0.25R expectancy
over n≥40 trades, we reopen US-evening on a fresh paper sample with slippage
model calibrated for evening volume. Decision framework: `mnq_session_hour_stress.md`.

## Lineage

- `docs/canonical_v1_verdict_full.md` — why NQ/crypto were killed
- `docs/mnq_feature_contribution_audit.md` — 20/80 axis analysis
- `docs/mnq_session_hour_stress.md` — session-hour stress analysis (RTH-only rule)
- `docs/overrides_p9_real_mnq_only_v2.json` — live override (regime_overlay=trending_only)
- `docs/live_launch_runbook.md` — step-by-step phase 1-6 runbook
- `docs/kill_log.json` → entry `APEX_PORTFOLIO_COMBINED_v1`
- `apex_predator/scripts/live_vs_paper_drift.py` — daily drift check
- `apex_predator/scripts/slippage_stress_mnq.py` — cost-robustness check
- `apex_predator/scripts/overlay_ablation_mnq.py` — overlay walk-forward validator
- `apex_predator/scripts/live_tiny_preflight_dryrun.py` — T1–T8 gates
- `apex_predator/configs/kill_switch.yaml` — automated halt config
