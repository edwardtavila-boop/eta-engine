# Red Team Review — D-series Apex Eval Hardening

**Date:** 2026-04-24 (v0.1.58) · updated 2026-04-24 (v0.1.59 residual-risk
closure) · updated 2026-04-24 (v0.1.63 R1 end-to-end wiring) · updated
2026-04-24 (v0.1.64 router-aware adapter).
**Scope:** D2 (`TrailingDDTracker`) and D3 (`ConsistencyGuard`) modules and
their wiring into `scripts/run_eta_live.py`.
**Reviewer:** `risk-advocate` agent (Opus 4.7, adversarial posture).
**Outcome (v0.1.58):** 3 BLOCKERs identified, all closed.
**Outcome (v0.1.59):** 4 HIGH residual risks re-litigated — 3 closed
(R2/R3/R4), 1 scaffolded with enforcement deferred to v0.2.x (R1).
**Outcome (v0.1.63):** R1 end-to-end wiring landed — ApexRuntime now
starts/stops the BrokerEquityPoller, feeds tier-A aggregate equity to
the reconciler each tick, logs every classification, and fires an
alert on the transition into `broker_below_logical`. All four HIGH
residual risks (R1/R2/R3/R4) are now CLOSED at the observation layer;
KillVerdict synthesis on sustained drift remains a v0.2.x scope call.
**Outcome (v0.1.64):** R1 deferred item #2 (router-aware poller
selection) closed — `RouterBackedBrokerEquityAdapter` proxies to
whichever futures venue the SmartRouter currently prefers so that an
IBKR↔Tastytrade failover keeps drift detection live instead of
silently degrading to `no_broker_data`. Single-source reconciler
contract preserved; +15 wiring tests.
**Outcome (v0.1.64 R1 production wire-up — adversarial dispatch):**
Mandatory Red-Team review of v0.1.63 R1 closure ran (risk-advocate
Opus 4.7) and returned **BLOCKED** with 3 BLOCKERs, 7 HIGH, 4 MEDIUM,
2 LOW, plus 6 process gaps. Two BLOCKERs closed in this bundle:
- **B1 (production wire-up gap)** — `_amain` was constructing
  `ApexRuntime(cfg)` with no broker-equity reconciler/poller kwargs,
  so the entire R1 stack was dormant code in live mode. Closed by
  `_build_broker_equity_adapter()` helper in `scripts/run_eta_live.py`
  that resolves IBKR-primary / Tastytrade-fallback / Null-degrade
  per the broker dormancy mandate, plus `make_poller_for(...)` /
  `BrokerEquityReconciler(...)` construction in `_amain`. Boot banner
  prints `broker_equity : <adapter_name> (tol_usd=... tol_pct=...
  refresh_s=...)` so misconfiguration is visible.
- **B2 (alert routing gap)** — `dispatcher.send("broker_equity_drift",
  ...)` was emitting an event that was not registered in
  `configs/alerts.yaml`. AlertDispatcher silently logs unknown events
  to `docs/alerts_log.jsonl` with no Pushover / email / SMS delivery,
  so the operator received zero notifications when drift fired. A
  re-audit (`scripts/_audit_alert_events.py`) found six other events
  with the same gap: `boot_refused`, `kill_switch_latched`,
  `apex_preempt`, `consistency_status`, `runtime_start`,
  `runtime_stop`, `bot_error`. All seven plus `broker_equity_drift`
  now have routing entries with appropriate levels. New CI gate
  `tests/test_alert_event_registry.py` walks every `dispatcher.send(
  EVENT, ...)` call site against the YAML registry and refuses to pass
  if any event is missing — this class of bug cannot recur.

Residuals from the v0.1.64 review (carry to v0.1.65 / v0.2.x):

| ID | Severity | Title | Owner |
|----|----------|-------|-------|
| B3 | BLOCKER  | tier-A aggregate equity invariant undocumented (sum-of-bot-state.equity vs single broker net-liq comparison may be apples-to-oranges) | v0.1.65 — needs design call on per-account vs per-fleet equity aggregation |
| H1 | HIGH     | No tolerance calibration harness — collected drift logs unread | v0.2.x — `scripts/calibrate_broker_drift_tolerance.py` |
| H2 | HIGH     | Asymmetric tolerances not modeled (below-bias different from above-bias) | v0.1.65 — split `tolerance_below_*` / `tolerance_above_*` |
| H3 | HIGH     | Transition-only alerting drops sustained-drift signal + threshold-jitter latch reset can spam | v0.1.65 — re-fire interval + hysteresis on latch clear |
| H4 | HIGH     | TTL is on our poll cycle, not broker server-side timestamp | v0.1.65 — parse server timestamps where available; identical-bytes detection where not |
| H5 | HIGH     | `ta_equity == 0` produces inf in JSON tick log (RFC 8259 violation) | v0.1.65 — guard `min_logical_usd` in tick + `as_dict` sentinel for inf |
| H6 | HIGH     | NullBrokerEquityAdapter in live mode is invisible to operator | v0.1.64 partial — boot banner now shows adapter name; v0.1.65 full — refuse-to-boot in live mode |
| H7 | HIGH     | Protocol "MUST NOT raise" guarantee is by convention, not enforced | v0.1.65 — `BrokerEquityAdapterBase` wrapper |
| M1 | MEDIUM   | No per-bot drift detection (aggregate-only) | v0.2.x with multi-account venue introspection |
| M2 | MEDIUM   | TrailingDDTracker/ConsistencyGuard run on logical equity, ignore reconciler output | v0.2.x — KillVerdict synthesis design |
| M3 | MEDIUM   | No `runtime_log.jsonl` rotation (8GB/month at 1s cadence) | v0.1.65 — daily rotation + gzip on age |
| M4 | MEDIUM   | No recorded broker-payload fixtures — IBKR/Tasty schema drift would silently break parsing | v0.1.65 — VCR-style fixture tests |
| L1 | LOW      | Adapter `name` not uniqueness-enforced | accepted — single-account today |
| L2 | LOW      | `ReconcileStats.max_drift_usd_abs` lifetime-only (never reset) | v0.1.65 — windowed max |

Process gaps from the review:
1. ✅ **Test stub bypassed real alert routing** — closed by
   `tests/test_alert_event_registry.py` which walks call sites against
   the production `alerts.yaml`.
2. **"Deferred to v0.2.x" docstring is the same in 4+ versions** — no
   exit criteria. v0.1.65 should commit a calibrator-script ticket
   with concrete acceptance criteria.
3. **No production wire-up smoke test** — should add
   `tests/test_amain_wire_up.py` that runs `_amain(["--max-bars", "1",
   "--dry-run"])` against a temp dir and asserts the `broker_equity`
   block lands in the JSONL.
4. **Roadmap-vs-code reconciler missing** — bump scripts advance the
   R-status flag without verifying the underlying code symbols exist.
   v0.1.65 should ship `scripts/_audit_roadmap_vs_code.py`.
5. **No operator runbook for drift detector** — v0.1.65 should add
   `docs/runbooks/broker_equity_drift_response.md`.
6. ✅ **`docs/red_team_d2_d3_review.md` design doc out of sync with
   v0.1.63 contract layer** — closed by this update.

This document captures the adversarial teardown of the D-series work, the
fixes shipped in response, and the residual risks that remain (documented
so they cannot be forgotten).

---

## Executive summary

Before this review the D-series modules had unit-level coverage but the
**wiring** into `run_eta_live.py` had three gaps that could let an Apex
eval fail silently. The review classified them as BLOCKERs because each
could cause the runtime to *appear* correct (all alerts green, all tests
green) while the eval was either already busted or about to bust.

| ID | Finding | Severity | Status |
|---:|---|---|---|
| B1 | UTC-midnight day-bucketing splits overnight equity-futures sessions across two Apex day keys | CRITICAL | Closed |
| B2 | Legacy `build_apex_eval_snapshot` fallback lacks freeze rule; silently under-protects in live mode when tracker is not wired | CRITICAL | Closed |
| B3 | 30%-rule VIOLATION was advisory-only: alert + log, no enforcement action on `is_paused` | HIGH | Closed |

Plus several HIGH findings that were accepted as known residual risk (see
"Residual risks" below).

---

## B1 — Session-day bucketing bug

**Finding.** `run_eta_live.py` was keying today's consistency-guard entry
by `utc_today_iso()`, which buckets PnL by **UTC calendar midnight**. The
Apex trading day is defined as the 24-hour window ending at **17:00
US/Central** (CME Globex close convention, DST-aware). An overnight
equity-futures session that generates PnL at 22:30 UTC in July (17:30 CDT)
would be charged to a *different* UTC day than PnL generated at 02:00 UTC
the next morning (21:00 CDT the prior evening) — even though both events
belong to the same Apex trading day.

Effect: the *largest winning day* appears smaller and the *total net
profit* denominator includes extra zero-PnL buckets. Both errors bias the
30%-rule ratio **downward**, hiding a real concentration risk. The
runtime reports "OK" or "WARNING" when the true state is "VIOLATION",
and the operator flies blind until Apex itself closes the eval.

**Fix.** Added `apex_trading_day_iso()` in
`eta_engine/core/consistency_guard.py`. Uses
`zoneinfo.ZoneInfo("America/Chicago")` to compute the 17:00 local
rollover in DST-aware fashion, with a fixed 23:00-UTC fallback when
`zoneinfo` is unavailable (wrong by ≤ 1h in summer, never splits RTH).
`run_eta_live.py` now calls this helper; `utc_today_iso()` stays with a
deprecation note for backwards compatibility.

**Tests.** `TestApexTradingDayIso` (11 tests): CDT + CST before and after
rollover, exact boundary at 17:00 CT, one-second-before boundary,
overnight-session co-location, naive-datetime coercion, `ZoneInfo`-absent
fallback, and an explicit diff-test confirming the two helpers disagree
on an evening-session timestamp (the bug the fix closes).

---

## B2 — Live-mode gate on `TrailingDDTracker`

**Finding.** `run_eta_live.py` runs a tick-precise trailing-DD tracker
**when one is supplied via the constructor kwarg**. When the kwarg is
omitted (which is the default), the runtime falls back to
`build_apex_eval_snapshot()` — a bar-level proxy that does **not**
implement the Apex freeze rule (once `peak >= start + cap`, the floor
locks at `start` forever). The fallback silently under-protects: a live
account that has climbed above the initial cap will compute a floor that
keeps trailing the peak down, so a normal retrace through the *correct*
frozen floor appears safe when it is actually a bust.

Effect: a runtime constructed without a tracker in `--live` mode is a
footgun. The operator has to remember to pass the tracker; nothing in
the framework enforces it.

**Fix.** `ApexRuntime.__init__` now raises `RuntimeError` when
`cfg.live=True AND cfg.dry_run=False AND trailing_dd_tracker is None`.
The error message names the missing wiring explicitly and points at the
module. Dry-run, paper-sim, and unit tests stay permissive (the proxy is
acceptable for those modes).

**Tests.** `TestLiveModeTrackerGate` (4 tests): live without tracker
raises, live with tracker builds cleanly, dry-run without tracker builds
cleanly, `live=True + dry_run=True` builds cleanly (dry_run wins).

---

## B3 — VIOLATION enforcement (advisory → pause)

**Finding.** When the consistency guard returned
`ConsistencyStatus.VIOLATION`, the runtime sent an alert and wrote a
structured log line. Neither action prevented new trades. A bot already
concentrated on its largest winning day could continue to open positions
until the operator noticed the alert in Discord/Slack and manually
paused. For an automated system, "notify and keep trading" is not
enforcement — it is a different kind of silent failure.

Effect: the guard was correctly detecting the risk but the runtime was
not acting on it. Tests covered the detection path but not an
enforcement path (because none existed).

**Fix.** On `VIOLATION`, the runtime now synthesizes a
`KillVerdict(action=PAUSE_NEW_ENTRIES, severity=CRITICAL, scope="tier_a")`
and feeds it through the existing `apply_verdict` dispatch path, which
flips `bot.state.is_paused = True` on every tier-A bot. Existing
positions are **not** flattened — they close on their own signals — but
new entries are blocked until the operator clears the violation (close
the bucket, bank the win, or `ConsistencyGuard.reset()` for a fresh
eval). The verdict is also appended to the tick's verdict log so audit
history captures the enforcement.

**Tests.** `TestConsistencyViolationPauses` (2 tests): pre-seeded
VIOLATION fires PAUSE on the tick and persists in `runtime.jsonl`;
pre-seeded WARNING does NOT fire PAUSE.

---

## Residual risks (v0.1.58 state → v0.1.59 closure)

The four HIGH findings below were flagged during the D-series Red Team
but originally deferred as "accepted residual risks" for v0.2.x. In
v0.1.59 we re-litigated that call — three of the four were within reach
and the fourth had a clean scaffold-now-wire-later shape. All four are
tracked below with their current closure state.

### R1 — Logical equity vs broker MTM  |  CLOSED (observation-only, v0.1.63)

**Original finding.** The tracker consumes `sum(bot.state.equity)` — a
logical figure maintained by the bot's own PnL book. Apex accounts for
MTM at broker level (unrealized + realized + funding + fees). A
prolonged disconnect between these two could drift the floor calculation
from what Apex sees.

**v0.1.59 scaffolding.** Added
`eta_engine/core/broker_equity_reconciler.py` —
`BrokerEquityReconciler` accepts a caller-supplied
`broker_equity_source: Callable[[], float | None]`, compares logical
equity to broker equity on every reconcile tick, and classifies drift
against configurable USD/pct tolerances. Classification taxonomy:
`within_tolerance` / `broker_below_logical` (dangerous — cushion
over-stated) / `broker_above_logical` (informational — cushion
under-stated, merely early flatten) / `no_broker_data` (in-tolerance by
convention since we can't assert drift we can't see). Source exceptions
are swallowed and classified as `no_broker_data`. The module does not
pause, flatten, or synthesize a KillVerdict — this is pure observation.

**v0.1.62 protocol layer.** Added
`eta_engine/core/broker_equity_adapter.py` — a
`@runtime_checkable typing.Protocol` (`BrokerEquityAdapter`) requiring
`name: str` and `async def get_net_liquidation() -> float | None`. Both
production adapters (`IBKRAdapter`, `TastytradeVenue`) satisfy the
protocol structurally without inheritance. Shipped alongside:
`NullBrokerEquityAdapter` (paper / dormant-venue placeholder) and
`make_poller_for(adapter, *, refresh_s, stale_after_s)` factory that
constructs but does not start a `BrokerEquityPoller` bound to the
adapter.

**v0.1.63 runtime wiring (closure).** `ApexRuntime.__init__` now accepts
two kwargs: `broker_equity_reconciler: BrokerEquityReconciler | None`
and `broker_equity_poller: BrokerEquityPoller | None`. When wired:

  * `run()` awaits `poller.start()` after the bot-start loop so the
    network session is not spun up for an abort on factory errors, and
    awaits `poller.stop()` FIRST in the `finally:` block (before
    `bot.stop()`) so a slow broker logout cannot delay live-bot
    draining.
  * `_tick()` now computes `ta_equity` ONCE at the top and reuses it
    for both the trailing-DD tracker (tracker.update input) and the
    reconciler (logical-side of drift check), preventing any rounding
    divergence between the two paths.
  * On every tick the reconciler's classification lands in the
    per-tick log (`runtime_log.jsonl` → `"broker_equity"` sub-key:
    `reason`, `in_tolerance`, `drift_usd`, `drift_pct_of_logical`).
  * Fan-out alert (`broker_equity_drift`) fires on the TRANSITION INTO
    `broker_below_logical` only — sustained drift does not spam the
    alert channel. Recovery clears the latch so a subsequent re-entry
    re-alerts. Other classifications (within_tolerance /
    broker_above_logical / no_broker_data) are silent on the alert
    channel but still land in the tick log.

**Why observation-only remains correct.** Synthesizing a KillVerdict on
out-of-tolerance would couple the drift classifier to the policy layer
that we deliberately keep inside `KillSwitch`. The reconciler gives the
operator a high-signal alert + audit trail; the operator decides
whether to pause / flatten / investigate. Promoting drift to a verdict
is a v0.2.x scope call (requires calibrating the tolerance against
commission + slippage empirics from live paper runs, not the synthetic
harness we ship today).

**v0.1.64 router-aware adapter (closure of deferred item #2).**
Added `RouterBackedBrokerEquityAdapter` to
`eta_engine/core/broker_equity_adapter.py` — a `BrokerEquityAdapter`
that consults `router.choose_venue(probe_symbol)` on every fetch and
proxies to whichever futures venue is currently active. The
reconciler / poller side keep their existing single-source contract;
the router takes care of substitution under the broker dormancy
mandate (IBKR primary, Tastytrade fallback, Tradovate dormant). A
mid-session failover swaps the read source automatically — no
poller re-wire, no drift-detection blackout. Three layers of
exception swallowing (router probe / venue lacks reader / reader
raises) all degrade to `None` so the reconciler classifies as
`no_broker_data` and the supervisor keeps running. Tests:
`TestRouterBackedBrokerEquityAdapter` (15 tests) covers protocol
fit, failover semantics, exception isolation, dormancy substitution,
end-to-end with reconciler, mid-polling failover.

**Still deferred to v0.2.x:** (1) KillVerdict synthesis on sustained
out-of-tolerance. (2) A multi-broker drift fan-out (cross-check
IBKR vs Tastytrade simultaneously). Neither is an Apex-eval blocker
— observation with router-aware single-broker reads is sufficient
for the single-account pattern Apex evals run on.

**Tests.**
  * `tests/test_broker_equity_reconciler.py` — 21 tests: no-data,
    within-tolerance, broker-below-logical, broker-above-logical,
    USD/pct boundaries, zero logical equity, raising source, stats
    counters, result-shape contract.
  * `tests/test_broker_equity_poller.py` — poller lifecycle, TTL
    staleness, error-swallow, counter semantics.
  * `tests/test_broker_equity_adapter.py` — protocol satisfaction,
    factory, null adapter.
  * `tests/test_run_eta_live.py::TestBrokerEquityReconcilerIntegration`
    — 6 new tests: (a) no reconciler = legacy path, (b) classification
    logged every tick, (c) alert fires once on transition, (d) no-data
    logged not alerted, (e) poller lifecycle (start + stop ordering),
    (f) drift clear → re-enter → re-alert.

### R2 — Tick-interval latency  |  CLOSED

**Original finding.** The runtime polls on a 5-second tick by default.
A fast retrace during that window could cross the floor before the next
update.

**v0.1.59 closure.** Added
`validate_apex_tick_cadence(...)` in
`eta_engine/core/kill_switch_runtime.py` — a pure-function validator
enforcing the invariant
`tick_interval_s * max_usd_move_per_sec * safety_factor <= cushion_usd`.
Default `max_usd_move_per_sec=300.0` and `safety_factor=2.0` bound the
worst-case single-tick retrace. Default `RuntimeConfig.tick_interval_s`
reduced **5.0 → 1.0**. In live mode (`live=True`) the validator raises
`ApexTickCadenceError` if the inequality fails; paper/dry-run no-ops.
`load_runtime_config()` calls the validator with the cushion read from
`kill_switch.tier_a.apex_eval_preemptive.cushion_usd`, so a mis-sized
config fails loudly at startup before a single tick runs.

**Tests.** `TestValidateApexTickCadence` (12 tests,
`test_kill_switch_runtime.py`) + `TestLoadRuntimeConfigTickCadence`
(4 tests, `test_run_eta_live.py`). Covers: invariant satisfied, fails
in live, no-op in paper, non-positive inputs rejected, default is 1.0s.

### R3 — Freeze-rule re-entrancy  |  CLOSED

**Original finding.** The tracker freezes when `peak >= start + cap`.
The risk is that if the tracker's state file is ever accidentally
deleted or the operator re-inits with a larger `trailing_dd_cap_usd`,
the freeze is lost and the floor resumes trailing.

**v0.1.59 closure.** Added `TrailingDDAuditLog` — an append-only JSONL
audit log co-located with the state file (default
`<state_path>.audit.jsonl`). `TrailingDDTracker` now emits immutable
events on every lifecycle transition: `init` (fresh create), `load`
(existing state), `freeze` (exactly once at the transition), `breach`
(each tick at/below floor), `reset` (with full `prior_state` snapshot,
operator name, reason). `append()` writes JSONL + fsyncs per append.
`reset()` now requires
`operator: str` (non-empty) and `acknowledge_destruction: bool=True` —
without the explicit ack the tracker raises
`ResetNotAcknowledgedError`. **Deleting the state file does not delete
the audit log**, so a forensic review can always detect a silent
re-init.

**Tests.** 6 new test classes in `test_trailing_dd_tracker.py`:
`TestAuditLogInitAndLoad`, `TestAuditLogFreezeAndBreach`,
`TestAuditLogSequenceMonotonicity`, `TestResetAcknowledgment`,
`TestAuditLogSurvivesStateDeletion`, `TestTrailingDDAuditLogUnit`.

### R4 — Session-day math vs weekends / holidays  |  CLOSED

**Original finding.** The 30% rule buckets by Apex trading day.
Weekends and US holidays don't exist in the calendar; the
`apex_trading_day_iso` helper keys a Saturday-morning timestamp to
"Saturday" which Apex probably ignores.

**v0.1.59 closure.** Added `eta_engine/core/events_calendar.py` —
CME Globex session calendar with `dateutil.easter`-driven Good Friday +
fixed-date closures (New Year, MLK, Presidents', Memorial, Juneteenth,
Independence, Labor, Thanksgiving, Christmas). `consistency_guard.py`
now routes `apex_trading_day_iso()` through the calendar so
Saturday/Sunday/holiday timestamps roll forward to the next regular
trading day instead of creating phantom buckets.

**Tests.** `test_core_events_calendar.py` covers the full CME calendar;
`test_consistency_guard.py` extended with rollover cases around each
closure type.

---

## Coverage delta

Before v0.1.58:
- `tests/test_consistency_guard.py` — 32 tests (guard logic, no
  session-day coverage).
- `tests/test_run_eta_live.py` — 60 tests (D2+D3 integration but no
  live-mode gate, no enforcement path).

After v0.1.58:
- `tests/test_consistency_guard.py` — **43 tests** (+11 `TestApexTradingDayIso`).
- `tests/test_run_eta_live.py` — **66 tests** (+4 `TestLiveModeTrackerGate`,
  +2 `TestConsistencyViolationPauses`).

After v0.1.59 (residual-risk closure):
- `tests/test_core_events_calendar.py` — **NEW** (R4: CME calendar).
- `tests/test_consistency_guard.py` — extended with calendar rollover cases.
- `tests/test_trailing_dd_tracker.py` — extended with 6 new audit-log
  classes (R3: init/load, freeze/breach, sequence monotonicity, reset
  acknowledgment, state-deletion survival, append-only unit).
- `tests/test_kill_switch_runtime.py` — +12 tests `TestValidateApexTickCadence` (R2).
- `tests/test_run_eta_live.py` — +4 tests `TestLoadRuntimeConfigTickCadence` (R2).
- `tests/test_broker_equity_reconciler.py` — **NEW**, 21 tests (R1).

Full regression: **3827 passed, 3 skipped** (Python 3.14.4 / Windows /
eta_engine) as of 2026-04-24. No regressions from v0.1.58 baseline.

---

## Quick-reference commands

```bash
# Single-module check
python -m ruff check eta_engine/core/consistency_guard.py \
                     eta_engine/scripts/run_eta_live.py

# D-series regression
python -m pytest \
    eta_engine/tests/test_run_eta_live.py \
    eta_engine/tests/test_consistency_guard.py \
    eta_engine/tests/test_trailing_dd_tracker.py \
    eta_engine/tests/test_kill_switch_latch.py \
    -x -q

# Chaos drills
python -m eta_engine.scripts.chaos_drills

# Kill-switch latch state
type eta_engine\state\kill_switch_latch.json

# Clear a tripped latch (requires operator name)
python -m eta_engine.scripts.clear_kill_switch --confirm --operator <name>
```
