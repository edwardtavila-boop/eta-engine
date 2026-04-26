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
| T1 | Preflight all required gates | `python -m eta_engine.scripts.live_tiny_preflight_dryrun` | 14/14 required PASS |
| T2 | Clock drift < 3s | grep `clock_drift` in T1 output | PASS |
| T3 | Paper baseline current | `ls -lt docs/paper_run_report_mnq_only_v2.json` | ≤ 7 days old |
| T4 | Journal directory writable | `touch docs/journals/live/.ok && rm $_` | no error |
| T5 | Active broker smoke | Phase 2 block from `live_launch_runbook.md` (IBKR `get_net_liquidation()` round-trip; Tradovate path is DORMANT and lives in Appendix A) | net-liq read OK |
| T6 | Market session | **RTH ONLY** 09:30–16:00 ET (see §13) | inside window |
| T7 | Kill-switch yaml loads | `python -c "import yaml; yaml.safe_load(open('eta_engine/configs/kill_switch.yaml'))"` | no error |
| T8 | Last reconcile GREEN | `python eta_engine/scripts/_trade_journal_reconcile.py --hours 24` | exit 0 |

If ANY is RED → DO NOT START. Fix or abort the session.

---

## 3. Block rules — WHEN TO HALT IN-SESSION (automated + manual)

### Automated kill-switch (runs continuously)
Trips `FLATTEN_ALL + CRITICAL` when any of:
- Realized portfolio DD ≥ 50% of session cap
- 5+ consecutive losers
- Active futures broker HTTP 5xx for 3 minutes (currently IBKR Client Portal Gateway; Tastytrade fallback if router has failed over; Tradovate is DORMANT)
- Telegram heartbeat silent > 2 minutes
- Local equity diverges from broker net-liq by > $50 (R1 broker-equity drift detector; see `core/broker_equity_reconciler.py`)

### Manual halt triggers (operator discretion)
- Live-vs-paper drift **RED** on any run of:
  `python eta_engine/scripts/live_vs_paper_drift.py --hours 24`
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
pkill -f run_eta_live
```

### Emergency: positions still open after supervisor gone
1. Open the **active broker's** UI:
   - **IBKR primary** (default): IBKR Client Portal web / Trader Workstation.
   - **Tastytrade fallback** (if router has failed over): Tastytrade web.
   - Tradovate is DORMANT — see `live_launch_runbook.md` Appendix A if it has un-dormanted.
2. Orders tab → **Cancel All Working**.
3. Positions tab → **Flatten All** (market).
4. Immediately: `python eta_engine/scripts/_trade_journal_reconcile.py --hours 1`.
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
python eta_engine/scripts/_trade_journal_reconcile.py --hours 24
python eta_engine/scripts/live_vs_paper_drift.py --hours 24 \
    --paper-baseline eta_engine/docs/paper_run_report_mnq_only_v2.json
python eta_engine/scripts/_kill_switch_drift.py --hours 24
python eta_engine/scripts/session_scorecard_mnq.py --mode live --hours 24 \
    --journal eta_engine/docs/journals/live/mnq_journal.jsonl \
    --paper-baseline eta_engine/docs/mnq_v2_trades.json
```

All three must exit 0. If any exits 1 (YELLOW), continue with caution. If any
exits 2 (RED), HALT next session until root cause fixed.

### Slow-bleed circuit breaker (added 2026-04-24, lesson #14)

The `kill_switch.yaml` `tier_a.per_bucket.<bot>.slow_bleed` block trips
FLATTEN_BOT when the rolling expectancy over the last `window_n_trades`
(default 20) drops below `expectancy_threshold_r` (default −0.10R), provided
at least `min_trades_for_check` trades (default 10) have been recorded.

| Param                  | Default | What it controls                                  |
|------------------------|---------|---------------------------------------------------|
| window_n_trades        | 20      | Lookback for rolling expectancy                   |
| min_trades_for_check   | 10      | Warm-up — no check below this trade count         |
| expectancy_threshold_r | -0.10   | Trip if rolling exp_R ≤ this                      |

This catches the regime-shift scenario the v2.2 NQ transfer test exposed
(`v2_2_nq_transfer.md` — Jan-Feb 2026 NQ data showed v2.2 bleeding −0.20R
per trade for ≥5 weeks). Daily-DD-20% global kill alone would not have
caught this; rolling-N-trade expectancy does.

The runtime feeds `recent_trade_rs` (per-trade R outcomes, ordered) into
`BotSnapshot.recent_trade_rs`. Empty list = warm-up / no check. The kill
switch is no-op if the YAML omits the `slow_bleed:` block.

---

## 8. Weekly check-list (every Sunday)

- Re-run bootstrap-CI on accumulated live sample:
  `python eta_engine/scripts/bootstrap_ci_real_bars.py --iterations 10000 --weeks 1 --seed 11 --symbols mnq --label mnq_live_wk<N>`
- Re-run walk-forward:
  `python eta_engine/scripts/walk_forward_real_bars.py --weeks 4 --stride-weeks 1 --max-folds 6 --symbols mnq --label mnq_live_wk<N>`
- Update sample-size tracker:
  `python eta_engine/scripts/sample_size_calc.py --report docs/bootstrap_ci_mnq_live_wk<N>.json --label mnq_live_wk<N>`

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
- Pre-flight network RTT check against the active broker (IBKR Client
  Portal Gateway primary, Tastytrade API fallback; Tradovate is DORMANT),
  30 samples: p95 > 800 ms demotes T-check to YELLOW. p95 > 1500 ms =
  HARD ABORT.

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

## 14. ATR-floor rule — v2.1 (superseded by v2.2, NOT live)

**v2.1 was the paper config 2026-04-24 until v2.2 shipped same-day.**
v2.1 remains the floor-only stepping-stone for lineage reconstruction.

v2.1 overrides: `overrides_p9_real_mnq_only_v2_1.json` adds
`atr_floor_ratio=1.0` + `atr_floor_lookback=8640` (30 days of 5-min bars).

**What it does:** suppresses entries on bars where current ATR(14) is below
the rolling-median ATR. Keeps only high-vol (panic) bars where the MNQ edge
actually lives. Implementation in `make_ctx_builder_real` — sets
`ctx["session_allows_entries"] = False` and `ctx["atr_floor_suppress"] = True`
on suppressed bars.

**One-window result on the v2 4-week sample:**

| metric       | v2.0 (floor=0) | v2.1 (floor=1.0) |
|---           |---:|---:|
| trades       | 63 | 37 |
| expectancy   | +0.111R | **+0.358R** (3.2×) |
| win rate     | 49.2% | 59.5% |
| total R      | +6.99 | +13.26 |
| max DD       | 2.47% | 2.18% |

**Walk-forward:** 4-week folds × 1-week stride on the full 6.6-week cache:

| floor | n_folds | pass_rate | mean_R |
|---:   |---:     |---:       |---:    |
| 0.00  | 3       | **0%**    | -0.050R|
| 1.00  | 3       | **33%**   | +0.051R|

**Rule (superseded — see §15):**
- v2.1 was the paper config from 2026-04-24 (superseded by v2.2 same-day).
- v2.1 remains useful as the floor-only stepping-stone and for ablation.
  The atr_floor wiring it introduced is still live in v2.2 and all
  future versions.
- If daily trade count is suspiciously low, check that ATR history is
  populating — the filter needs 30 ATR samples before it activates
  (`len(atr_history_cache) >= 30`).

**Re-validation gate:** the live-promotion decision is attached to v2.2 §15,
not v2.1. v2.1 won't be re-validated separately.

**Re-sweep within v2.2 stack (2026-04-24):** `atr_floor_ratio` swept over
{0.0, 0.7, 1.0, 1.3} with `dow_blacklist=[3]` ON. Walk-forward inverted-U
peaks at 1.0 (mean +0.156R, 3/3 folds pass). Ablating to 0.0 drops to
+0.040R / 1/3. Confirms `atr_floor_ratio=1.0` is still optimal post-DOW;
both filters are independently load-bearing. See
`docs/atr_floor_validation_v2_2.md` for the 2×2 corner table.

Details: `docs/atr_floor_validation.md` (original v2.1 study) +
`docs/atr_floor_validation_v2_2.md` (v2.2-stack re-sweep + 2×2 ablation).

## 15. Thursday blacklist — v2.2 paper config (NOT live)

**v2.2 is the canonical MNQ paper config from 2026-04-24 onwards.**
Supersedes v2.1. File: `overrides_p9_real_mnq_only_v2_2_dow_thu.json`.

**What it adds on top of v2.1:** `dow_blacklist=[3]` (Thursday, ISO weekday).
Thursday MNQ entries are suppressed at the context-builder level
(`session_allows_entries=False` + `dow_suppress=True`). Implementation in
`make_ctx_builder_real`'s `_wrap_dow` — outermost wrapper, short-circuits
before the ATR-floor cache work.

**Motivation:** v2.0 n=16 Thursdays = -0.321R (31% win); v2.1 n=6 Thursdays =
-0.264R. Thursday is the only consistently net-negative weekday across both
configs. Candidate causal story (not proven): Thursday clusters macro releases
and Friday-expiry positioning, and the 8-axis confluence engine has no news
awareness.

**One-window result on the 4-week sample:**

| metric       | v2.1 (floor only) | v2.2 (floor + Thu blacklist) |
|:---          |---:               |---:                          |
| trades       | 37                | 31                           |
| expectancy   | +0.358R           | **+0.479R** (+34%)           |
| win rate     | 59.5%             | 64.5% (+5.0 pp)              |
| max DD       | 2.18%             | 2.03%                        |
| Sharpe       | 4.61              | 6.31                         |

**Walk-forward:** 4-week folds × 1-week stride, n_folds=3:

| config          | n_folds | pass_rate   | mean_R   |
|:---             |---:     |---:         |---:      |
| v2.1            | 3       | 33%         | +0.051R  |
| **v2.2 (Thu blacklist)** | 3 | **100%** | **+0.156R** |

**All three folds improved.** Two folds flipped sign (-0.008→+0.093,
-0.018→+0.122). Third fold strengthened (+0.179→+0.252). No fold worsened.

**Slippage stress** (post-hoc tick-level adverse slippage):
v2.2 survives 2 ticks/side at +0.4535R, still above the +0.05R paper gate.
Break-even is outside the tested range.

**Rule:**
- v2.2 is the **paper config from 2026-04-24 onwards**. Supersedes v2.1.
- v2.2 stays in **paper only**. 3 folds of walk-forward is too thin even at
  100% pass to trip the live gate (which requires ≥6 folds).
- Trade cadence drops another ~15% vs v2.1: expect ~2.1 trades/week (was
  ~2.5). Live-tiny calendar-time to n=120 extends from ~20 weeks to
  ~25 weeks on current cadence.
- **Do NOT further tighten threshold on top of v2.2.** The v2.3 candidate
  (threshold=5.5 on v2.2) projects to n=10, -0.10R — score-bucket
  non-monotonicity means threshold and Thu-blacklist are overlapping
  filters. See `docs/v2_2_dow_filter_validation.md` §Compound candidate
  v2.3 killed.

**Re-validation gate:** once the TradingView cache reaches ≥13 weeks of
MNQ 5-min bars, re-run `dow_filter_validation.py`. Promote to live only
if v2.2 pass_rate ≥ 67% across ≥6 folds.

Details: `docs/v2_2_dow_filter_validation.md`.

## Lineage

- `docs/canonical_v1_verdict_full.md` — why NQ/crypto were killed
- `docs/mnq_feature_contribution_audit.md` — 20/80 axis analysis (+ vol_regime miscalibration flag)
- `docs/mnq_session_hour_stress.md` — session-hour stress analysis (RTH-only rule)
- `docs/mnq_latency_volatility_interaction.md` — panic-ATR edge concentration
- `docs/atr_floor_validation.md` — v2.1 ATR-floor filter validation + ship decision
- `docs/atr_floor_validation_v2_2.md` — v2.2-stack re-sweep + 2×2 corner ablation (2026-04-24)
- `docs/v2_0_vs_v2_1_comparison.md` — v2.0 vs v2.1 dimension-by-dimension comparison
- `docs/v2_1_trade_distribution.md` — v2.1 trade-log distribution analysis (Thu blacklist motivation)
- `docs/v2_2_dow_filter_validation.md` — v2.2 Thursday blacklist validation + ship decision
- `docs/v2_2_dow_placebo.json` — DOW placebo sweep (2026-04-24); Mon-blacklist comparable to Thu
- `docs/v2_2_nq_transfer.md` — cross-symbol regime-shift finding (lesson #14)
- `docs/v2_2_promotion_attack.md` — adversarial agent review (risk-advocate + quant-researcher → HOLD)
- `docs/v2_2_block_bootstrap.md` — block-bootstrap CI on v2.2 (gate #3 fail evidence)
- `docs/v2_2_walk_forward_ci_corrected.md` — overlap-corrected fold-mean CI (gate #2 fail evidence)
- `docs/v2_2_dsr.md` — DSR computation (gate #4 fail by ~12×)
- `docs/v2_2_threshold_wf_sensitivity.md` — gate #8 PASS for confluence_threshold
- `docs/v2_2_atr_stop_mult_wf_sensitivity.json` — gate #8 PASS for atr_stop_mult
- `docs/v2_2_target_r_multiple_wf_sensitivity.json` — gate #8 PASS for target_r_multiple (tied)
- `docs/v2_2_max_trades_per_day_wf_sensitivity.json` — gate #8 PASS for max_trades_per_day
- `docs/v2_2_gate_8_knob_summary.md` — aggregate gate #8 PASS (4/4 knobs at WF argmax)
- `docs/trial_log.json` — cumulative N_trials log for DSR (gate #4)
- `docs/overrides_p9_real_mnq_only_v2.json` — v2.0 (trending_only, no floor)
- `docs/overrides_p9_real_mnq_only_v2_1.json` — v2.1 intermediate (floor=1.0, no DOW)
- `docs/overrides_p9_real_mnq_only_v2_2_dow_thu.json` — **v2.2 paper config** (floor=1.0 + Thu blacklist, not live)
- `docs/live_launch_runbook.md` — step-by-step phase 1-6 runbook
- `docs/kill_log.json` → entries `APEX_PORTFOLIO_COMBINED_v1` + `MNQ_V2_FAMILY_CACHE_CONTAGION`
- `eta_engine/scripts/live_vs_paper_drift.py` — daily drift check
- `eta_engine/scripts/slippage_stress_mnq.py` — cost-robustness check
- `eta_engine/scripts/overlay_ablation_mnq.py` — overlay walk-forward validator
- `eta_engine/scripts/live_tiny_preflight_dryrun.py` — T1–T8 gates
- `eta_engine/scripts/block_bootstrap.py` — gate #3 implementation (moving-block bootstrap)
- `eta_engine/scripts/walk_forward_ci.py` — gate #2 implementation (overlap-corrected CI)
- `eta_engine/scripts/trial_counter.py` — gate #4 implementation (cumulative N_trials + DSR)
- `eta_engine/scripts/v2_2_knob_wf_sensitivity.py` — gate #8 implementation (knob WF sweep)
- `eta_engine/scripts/v2_2_trade_dump.py` — full-sample trade dumper (feeds bootstrap/DSR)
- `eta_engine/scripts/v2_2_dow_placebo.py` — DOW methodology placebo
- `eta_engine/scripts/v2_2_nq_transfer.py` — cross-symbol transfer harness
- `eta_engine/configs/kill_switch.yaml` — automated halt config (now with `slow_bleed:` block)
- `eta_engine/core/kill_switch_runtime.py` — slow-bleed circuit breaker (lesson #14, #19)
- `eta_engine/docs/macro_calendar.json` — news_blacklist filter calendar (lesson #22)
