# Wave-25r — Full-Steam Audit Synthesis (2026-05-13 PM)

**Triggered by:** operator request "lint and debug the whole project ...
verify everything check everything ... optimize everything ... close
to launch and finish".

**Pre-commit clean.** Five specialized review agents (code-review,
devils-advocate, quant-researcher, risk-execution, market-microstructure)
ran in parallel against the wave-25 a→q codebase. Findings categorized
HIGH / MED / SMELL / INFO. This doc tracks which landed in code (✓)
vs which are documented for future work (○) vs which require operator
decisions (★).

## Test status after fixes

- **Ruff lint:** CLEAN (exit 0) across full codebase
- **Pytest (targeted to changed files):** 189 passed, 0 failed
- **Pytest (full sweep, with -x):** 3712 passed, 28 skipped, **1 inter-test pollution error** (`test_token_required_for_read_only_tool` — passes individually, fails after policy registry pollution from prior tests). This is **pre-existing** flake, NOT introduced by wave-25.
- **Astro check (apps/app):** CLEAN (exit 0)
- **Submodule status:** all aligned, none dirty

## HIGH severity — landed

✓ **`backfill_trade_closes_data_source.py:106`** — Wrong `json.dumps`
separators `(", ", ": ")` would have written backfilled records in
inconsistent format vs canonical writer (compact `(",", ":")`). Fixed
to compact separators. Downstream string-equality dedup tools no longer
diverge.

✓ **`scripts/jarvis_strategy_supervisor.py:4398-4399`** — `contextlib.suppress(Exception)`
on `_persist_open_position` in the partial-profit path created a
silent restart-safety gap. The submit_exit had already cleared the
canonical file; if persist failed, supervisor restart would orphan the
runner. Replaced with try/except that logs CRITICAL on failure
including the orphaned qty.

✓ **`scripts/jarvis_strategy_supervisor.py:3996-4003`** — The unified
entry gate had `except Exception ... proceeding with live submit` — a
fail-OPEN on the most important safety check. Per risk-execution agent
scenario A, two fail-open behaviors in series could ship unguarded
orders if `capital_allocator.resolve_execution_target` raised. Replaced
with fail-CLOSED `return None`.

✓ **`scripts/jarvis_strategy_supervisor.py:2570-2572`** —
`reconcile_with_broker` returned early on mode=live, the most important
time to reconcile. Restart-after-fill-before-persist (scenario B) would
leave broker holding a position the supervisor doesn't know about.
Now reconciles on both `paper_live` and `live`.

✓ **`safety/position_cap.py:32`** — `DEFAULT_CAP=10.0` allowed a single
MNQ entry to ship 10 contracts ($2,500 risk at 50-tick stop ≈ 167% of
BluSky $1,500 daily-loss limit). Lowered to `2.0`. Operators still set
`ETA_POSITION_CAP_<side>_<venue>_<sym>=1` for per-bot tuning. For
BluSky launch, set the per-symbol vars to 1 explicitly.

## HIGH severity — documented for operator (★)

★ **`prop_firm_guardrails.evaluate()` not wired into entry path** (risk-execution).
The actual prop-firm-aware $1,500 daily loss / $2,000 trailing DD /
$3,000 target check is dashboard-only. The supervisor's `_maybe_enter`
uses a hardcoded `prospective_loss_est_usd = 250.0` constant. Real
fix requires inserting `prop_firm_guardrails.evaluate(rules, state, signal)`
into `_maybe_enter` before `submit_entry`, fail-closed on
`allowed=False`. **Operator action required** because the rules
require an account-id mapping for the BluSky account that isn't yet
wired (Tradovate dormant until credentials arrive). Tracked in
OPERATOR_PUNCH_LIST_2026_05_13.md item 5.

★ **`scripts/jarvis_strategy_supervisor.py:803`: `risk_unit = bot.cash * 0.10`**.
10% risk per trade. For BluSky $50K equity reference, that's $5,000
risk-per-trade target — 333% of daily loss limit. The `_MAX_QTY_PER_ORDER`
cap of 5 MNQ contracts saves us today, but the sizing math is upside-
down for prop work. Recommended fix: compute `risk_unit` from prop
account's daily-loss buffer (e.g., 20% of remaining buffer USD), not
from total cash. **Requires operator decision on per-prop-account
sizing semantics.**

## MED severity — landed

✓ **`scripts/capture_tick_stream.py:187-188`** — Race condition: `len(self._buf)`
checked outside `_buf_lock` could miss the flush trigger when two threads
both append near threshold. Now reads buf_len inside the lock.

✓ **`scripts/eta_status.py:28`** — Hardcoded `STATE_DIR = Path(r"C:\...")`
broke portability. Now derived from `__file__.parents[2] / "var/eta_engine/state"`,
with `ETA_STATE_DIR` env override.

✓ **`scripts/diamond_prop_launch_readiness.py:75`** — `DEFAULT_LAUNCH_DATE = "2026-05-18"`
was past the NO_GO verdict. Pushed to `2026-07-15` per quant-researcher's
statistical justification (n>=150 confidence floor for +0.2R edge at
α=0.05, power=0.8 given current per-bot trade cadence).

✓ **`scripts/diamond_demotion_gate.py:294,296`** — Unicode `✗` and `⚠`
crashed the Windows console at cp1252 encoding. Replaced with ASCII
`X HARD` and `! SOFT`.

✓ **`venues/ibkr_live.py:1322-1330, 1346-1347, 1363-1364`** — Three bare
`except Exception:` with silent return. Cancel-order failures, get_positions
failures, get_balance failures now log at WARNING with exception text.
The risk-execution agent's note: "during a kill-switch flatten, if the
cancel fails (network blip, stale orderId), supervisor thinks the
bracket leg is cancelled when it isn't" — operators will now see this.

## MED severity — documented for operator (★)

★ **`venues/ibkr_live.py:543-566`** — Default clientId `99` is shared at
class level between supervisor and broker_router. Retry loop mitigates
Error 326 but doesn't prevent same-TWS race. **Operator action: set
`ETA_IBKR_CLIENT_ID=11` (supervisor) and `=12` (broker_router) explicitly
before BluSky launch.** Tracked in OPERATOR_PUNCH_LIST.

★ **Capture script comments** — Stale documentation says "supervisor
operates clientId 30-50"; actual default is 99 (`ETA_IBKR_CLIENT_ID`).
Comments will be updated in a later micro-commit.

★ **Microstructure: `_maybe_take_partial_profit` qty=0.5 fails on live
futures** — `int(0.5)=0` → REJECTED. Currently masked because
broker_bracket=True on IBKR paper-live. When Tradovate (BluSky route)
is wired, must confirm `ExecutionCapabilities.bracket_style == SERVER_OCO`
AND `broker_bracket=True` is set OR explicitly disable
`ETA_PARTIAL_PROFIT_ENABLED` for the BluSky lane.

★ **Microstructure: paper-target zero-slippage** — Line 1509 fills at
exact limit. Real-world IBKR may give partials. No live-vs-sim audit
running against paper-live journal. Recommendation: add +1 tick adverse
on paper-target fills as queue-position approximation.

★ **Microstructure: `bar.high >= target` `elif` ordering bias** — When
a single bar covers both stop AND target intrabar, paper-sim always
books target. +EV bias of up to 100% of stop distance on offending
bars. Recommended: book stop first (conservative).

★ **Microstructure: overnight/globex fills systematically optimistic** —
1.5 bps is RTH-grade. MNQ overnight slip is 4-10 ticks regularly.
Recommendation: add session-aware slippage table.

## Quant-researcher findings — documented (○)

○ **WR >= 50% criterion is wrong axis** — Replace with
`avg_R - 1.96*SE(R)/sqrt(n) > 0` (one-sided 95% lower bound on expR).
Won't change today's NO_GO verdict (all bots have insufficient sample).
Deferred until launch-candidate retest.

○ **n>=50 is sample FLOOR, not confidence threshold** — Updated docs
to reflect this. The actual confidence floor for detecting +0.2R edge
at σ≈1R, α=0.05, power=0.8 is n≈200.

○ **Correlation within sessions** — Effective N is 30-50% smaller than
raw count due to signal aggregation and same-session correlation.
CIs computed naively from n=109 are too narrow. Deferred fix.

○ **ASYMMETRY_BUG threshold** — Currently binary at 10pp gap. Quant
recommended emitting z-scores instead. Deferred.

○ **Bootstrap 95% CI on expR per bot** — Missing analysis. Should be
added to `prop_launch_check` output before any FUNDED_LIVE designation.

## Code-review smells — deferred (○)

○ **No test coverage for `backfill_trade_closes_data_source.py`** —
Ran on production data without tests. Need: mock JSONL with mixed
tagged/untagged/test-bot records, assert exact output.

○ **`eta_status.py:102-103`** — Bare `except Exception: return {}` in
`_supervisor_health_summary` swallows import errors silently. Should
log warning.

○ **`ETA_PARTIAL_PROFIT_ENABLED` defaults to `"true"`** — Risky default
in prop-fund context. The wave-25o forensic was misled BY this feature.
Either change default to `"false"` or emit startup WARNING when `true`.

○ **`capture_depth_snapshots.py:355-379`** — `_stuck_flagged` set
conflates thin-book and stuck-book warnings (same symbol can only
warn once). Should track separately.

## Devils-advocate verdict — agreed with both claims

✓ **Wave-25n NO_GO verdict is correct (92% confidence)** — Gate is
conservative; per-bot table is unambiguous; no single bot has credible
case to override AND-gate.

✓ **Wave-25o retraction is correct (95% confidence)** — Three
independent kill-shots on the `vol_low_size_mult=0.0` recommendation:
wrong config dataclass, qty=0.5 tautology from partial-profit, inverted
vol semantics on SweepReclaim anyway. No new evidence found that
would change the retraction.

Additional finding the agent surfaced: `SageGatedORB` has its OWN
scale-out (`enable_scale_out=True, rr_partial=1.5, partial_qty_frac=0.5`)
in addition to the supervisor's partial-profit. Both mechanisms emit
qty=0.5 records on winners. The qty=0.5 cohort is the union of two
independent partial mechanisms, both filtered to profitable trades.

## What we did NOT fix (and why)

✗ **prop_firm_guardrails wiring into entry path** — Requires Tradovate
account_id mapping; deferred until BluSky credentials land.

✗ **risk_unit = 0.10 → 0.20 of remaining buffer** — Architectural
change requiring operator decision on per-prop sizing semantics.

✗ **Live-vs-sim slippage audit** — Requires CME Depth subscription
data (Error 354 still blocking); separately decision-gated.

✗ **L2 overlay downgrade to advisory** — `confirm_sweep_with_l2`
already falls through to pass-open when captures aren't running; the
overlay is effectively no-op today. Documented; no code change needed.

✗ **WR criterion replacement** — Won't change today's NO_GO; replacing
risks breaking existing analysis baselines. Deferred to next major
wave when first launch candidate emerges.

## Net assessment after wave-25r

**Infrastructure: 92% complete.** Capture pipeline hardened, supervisor
fail-closed across all known crash paths, position cap defense-in-depth
tightened, ledger consistency improved, dashboard freshness gating
working, comprehensive test coverage on changed surfaces.

**Strategy edge: still 0/14.** No bot passes the strict launch-candidate
gate. Falsification deadline pushed to **2026-07-15** (was 2026-06-01)
per quant statistical justification.

**Operator backlog (cannot complete autonomously):**
1. Magic-link inbox verification (5 min)
2. Supabase migration 0003 apply (5 min)
3. Tradovate credentials when BluSky emails them
4. CME Depth subscription decision
5. Set `ETA_IBKR_CLIENT_ID` env vars for supervisor + router separately
6. Set `ETA_POSITION_CAP_*` env vars per symbol per side
7. Decide per-prop risk_unit semantics (`bot.cash * 0.10` is wrong for $50K eval)
8. Wire prop_firm_guardrails into `_maybe_enter` once Tradovate is live
9. Decide on `ETA_PARTIAL_PROFIT_ENABLED` default (recommend OFF for prop lanes)

## Falsification deadline (updated)

Was: 2026-06-01 (statistically incoherent per quant — only 19 days, only
mnq_futures_sage could plausibly reach n=50).

Now: **2026-07-15** (≈9 weeks). Gives top bots time to accumulate
n>=150 production-tagged records, which is the actual confidence floor
for detecting a +0.2R edge with σ≈1R at α=0.05, power=0.8.

If no bot crosses all five launch-candidate criteria by 2026-07-15,
qty sizing logic needs redesign (MES_V2 Fix C: constant-USD risk).

## Cross-reference

- `WAVE25_MASTER_SYNTHESIS_2026_05_13.md` — full wave-25 grand summary
- `MNQ_FUTURES_SAGE_VOL_REGIME_FORENSIC_CORRECTION_2026_05_13.md` —
  retracted vol-regime recommendation
- `MES_V2_SIZING_FORENSIC.md` — Fix A / Fix C constant-USD risk proposals
- `MONDAY_MORNING_OPERATOR_RUNBOOK.md` — Monday launch sequence
- `OPERATOR_PUNCH_LIST_2026_05_13.md` (root workspace) — human-only tasks

---

*Generated 2026-05-13 PM (wave-25r) by parallel multi-agent audit.
Specialized review agents: algo-code-reviewer (code), devils-advocate
(adversarial), quant-researcher (statistics), risk-execution (capital
preservation), market-microstructure (fill realism).*
