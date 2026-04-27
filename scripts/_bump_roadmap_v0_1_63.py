"""One-shot: bump roadmap_state.json to v0.1.63.

R1 END-TO-END RUNTIME WIRING -- the last mile on the R1 residual risk.
v0.1.59 shipped the BrokerEquityReconciler; v0.1.62 formalised the
adapter contract via a runtime-checkable Protocol + NullBroker stub +
make_poller_for factory. But neither bundle actually wired the
reconciler into the live runtime tick -- ApexRuntime never called
reconcile(), so R1 stayed classified as SCAFFOLDED (enforcement
deferred). v0.1.63 closes that: run() now awaits poller.start(),
the finally block awaits poller.stop() (ahead of bot.stop to avoid
a slow broker logout stalling live-bot draining), and _tick() feeds
tier-A aggregate equity through the reconciler on every cycle.

What ships
----------
scripts/run_eta_live.py (EDIT)
  * ApexRuntime.__init__ accepts two new kwargs:
    - broker_equity_reconciler: BrokerEquityReconciler | None = None
    - broker_equity_poller: BrokerEquityPoller | None = None
    Both stored as instance attrs for _tick and the lifecycle to read.
  * run() awaits self.broker_equity_poller.start() AFTER the bot-start
    loop so a bot-factory abort never spins up a network pinger.
  * run()'s finally block awaits self.broker_equity_poller.stop()
    FIRST (before bot.stop() loop) so a slow broker logout cannot
    delay the draining of live bots.
  * _tick() computes `ta_equity = sum(s.equity_usd for s in snapshots
    if s.tier == "A")` ONCE at the top and reuses it for both
    tracker.update and reconciler.reconcile -- a single source of
    truth prevents a rounding divergence between the two paths.
  * _tick() invokes self.broker_equity_reconciler.reconcile(ta_equity)
    when the reconciler is wired, captures the ReconcileResult, and:
      - logs every classification to runtime_log.jsonl under the
        tick entry's "broker_equity" sub-key (reason, in_tolerance,
        drift_usd, drift_pct_of_logical)
      - fires a `broker_equity_drift` alert only on the TRANSITION
        INTO "broker_below_logical" (sustained drift does not spam
        the alert channel; recovery clears the latch so a subsequent
        re-entry re-alerts)
  * self._last_broker_drift_reason: str | None state cache added to
    track transitions across ticks.

tests/test_run_eta_live.py (EDIT)
  * New class TestBrokerEquityReconcilerIntegration (6 tests):
    - test_no_reconciler_attached_is_noop: legacy path, no
      broker_equity key in tick entries.
    - test_reconciler_logs_classification_each_tick: fixed-source
      reconciler + within-tolerance -> each tick logs classification;
      stats counters reflect checks_total / checks_in_tolerance.
    - test_reconciler_alert_fires_once_on_drift_transition: broker
      below logical -> exactly ONE broker_equity_drift alert across
      3 ticks (transition behaviour), evidence includes logical/
      broker/drift.
    - test_reconciler_no_broker_data_is_logged_not_alerted: source
      returns None -> checks_no_data counter advances, no alert, tick
      log carries reason=no_broker_data.
    - test_poller_lifecycle_started_and_stopped_by_runtime: BrokerEquity
      Poller wired -> fetch counter advances (eager fetch on start),
      poller.is_running() False after runtime.run() completes.
    - test_drift_transition_resets_and_refires: script broker values
      to 5000/4000/5000/4000 across 4 ticks -> 2 alerts fire (entry
      and re-entry after recovery), not 1.

docs/red_team_d2_d3_review.md (EDIT)
  * R1 status flipped: "SCAFFOLDED (enforcement deferred)" ->
    "CLOSED (observation-only, v0.1.63)".
  * Executive summary updated with v0.1.63 outcome line.
  * R1 section expanded with v0.1.63 runtime wiring details +
    tests + rationale for staying observation-only (KillVerdict
    synthesis deferred to v0.2.x pending live-paper empirics).

Scope discipline
----------------
  * No change to KillSwitch / KillVerdict policy. Reconciler stays
    observation-only; we surface the drift, the operator decides.
  * No change to which broker is the equity source. Supervisor still
    picks its source explicitly; router-aware poller selection is
    v0.2.x.
  * No multi-broker fan-out. Single-account on Apex evals.

Regression
----------
  * pytest -p no:randomly -q: 3950 passed, 10 skipped, 1 warning.
  * In-scope delta vs v0.1.62: +6 new tests in
    TestBrokerEquityReconcilerIntegration. Remaining +24 come from
    out-of-scope test additions in adjacent modules that landed
    alongside this work (primarily adapter protocol suite rollup).
  * Ruff: clean on both files touched (run_eta_live.py +
    test_run_eta_live.py).

Expected state changes
----------------------
  * roadmap_state.json version bumped to v0.1.63.
  * eta_engine_tests_passing updated to 3950.
  * New key eta_engine_v0_1_63_r1_end_to_end with the full ledger.
  * New milestone appended.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.63"
NEW_TESTS_ABS = 3950
R1_TESTS_ADDED = 6


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_63_r1_end_to_end"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "R1 END-TO-END RUNTIME WIRING -- close the last mile on "
            "R1 by wiring BrokerEquityPoller + BrokerEquityReconciler "
            "into ApexRuntime's run()/finally/_tick paths. Observation-"
            "only; KillVerdict synthesis stays a v0.2.x scope call."
        ),
        "theme": (
            "v0.1.59 shipped the drift detector; v0.1.62 formalised "
            "the adapter contract. Neither bundle called reconcile() "
            "from the live tick -- so R1 was still classified as "
            "SCAFFOLDED. v0.1.63 closes that: ApexRuntime now starts/"
            "stops the poller, feeds tier-A aggregate equity through "
            "the reconciler on every cycle, logs each classification, "
            "and fires an alert on transition into broker_below_"
            "logical. Recovery clears the latch so re-entries re-alert."
        ),
        "operator_directive_quote": "do everything else you can",
        "modules_edited": [
            "eta_engine/scripts/run_eta_live.py (ApexRuntime "
            "accepts broker_equity_reconciler + broker_equity_poller "
            "kwargs; run() manages poller lifecycle; _tick() computes "
            "ta_equity once and feeds it to both tracker + reconciler; "
            "transition-tracked broker_equity_drift alert).",
            "eta_engine/docs/red_team_d2_d3_review.md (R1 status "
            "SCAFFOLDED -> CLOSED; executive summary updated with "
            "v0.1.63 outcome line; R1 section expanded with wiring + "
            "tests + observation-only rationale).",
        ],
        "tests_added": [
            "eta_engine/tests/test_run_eta_live.py::"
            "TestBrokerEquityReconcilerIntegration (6 tests across "
            "the wiring contract: no-reconciler noop, classification "
            "logged each tick, alert fires once on transition, "
            "no-broker-data logged-not-alerted, poller lifecycle, "
            "transition clear + re-fire).",
        ],
        "design_choices": {
            "single_ta_equity_computation": (
                "_tick() now computes ta_equity = sum(s.equity_usd "
                "for s in snapshots if s.tier == 'A') exactly once at "
                "the top and reuses it for both tracker.update and "
                "reconciler.reconcile. Previously ta_equity was "
                "computed only inside the tracker branch. A single "
                "source of truth prevents a rounding divergence "
                "between the two paths."
            ),
            "poller_start_after_bot_start": (
                "run() awaits poller.start() AFTER the bot-start "
                "loop so that a bot-factory crash never spins up a "
                "network pinger for a runtime about to abort."
            ),
            "poller_stop_before_bot_stop": (
                "The finally block awaits poller.stop() FIRST, "
                "before the bot.stop() loop. A slow broker logout "
                "would otherwise delay the draining of live bots. "
                "Reconciler is observation-only, so a slightly-stale "
                "poller at the very end of shutdown is harmless."
            ),
            "transition_only_alerting": (
                "broker_equity_drift fires on the TRANSITION INTO "
                "broker_below_logical, not every tick while drift "
                "persists. Steady-state drift is captured in the "
                "tick log for the audit trail but does not spam the "
                "operator's alert channel. Recovery clears the latch "
                "so a subsequent re-entry re-alerts -- a recover + "
                "re-drift pattern is distinct operator-signal, not "
                "noise."
            ),
            "observation_only_on_purpose": (
                "The reconciler does not synthesize a KillVerdict. "
                "Drift classification is intentionally decoupled from "
                "the policy layer that lives in KillSwitch. Promoting "
                "sustained drift to a verdict requires tolerance "
                "calibration from live-paper empirics (commissions, "
                "slippage, funding, carry) rather than the synthetic "
                "harness we ship today. v0.2.x scope."
            ),
            "classification_in_tick_log": (
                "Every tick's classification lands in "
                "runtime_log.jsonl under the 'broker_equity' sub-key. "
                "This preserves the full drift history for post-"
                "session audit even when the alert channel is quiet "
                "(within_tolerance / broker_above_logical / "
                "no_broker_data all go through the log-only path)."
            ),
        },
        "scope_exclusions": {
            "no_killswitch_synthesis_on_drift": (
                "Reconciler stays observation-only. KillVerdict "
                "synthesis on sustained out-of-tolerance is v0.2.x "
                "scope once we have live-paper tolerance empirics."
            ),
            "no_router_aware_poller_selection": (
                "Supervisor-level 'which broker's poller to wire' "
                "policy is not yet in place. v0.1.63 accepts the "
                "poller/reconciler as kwargs so the current wiring "
                "(explicitly picked at the call site) can be "
                "swapped for a router-driven selector in v0.2.x."
            ),
            "no_multi_broker_drift_fanout": (
                "Apex evals are single-account. Cross-broker drift (IBKR vs Tastytrade) is a future-state concern."
            ),
            "no_replace_logical_with_broker_mtm": (
                "The tracker still consumes logical equity. Whether "
                "to FEED the tracker with broker_mtm - sum(open_pnl) "
                "once the drift check is live is a venue-integration "
                "choice deferred to v0.2.x."
            ),
        },
        "r1_closure_state": {
            "v0_1_58": "HIGH residual risk, accepted for v0.2.x.",
            "v0_1_59": ("SCAFFOLDED -- BrokerEquityReconciler module added + 21 tests. No runtime call site yet."),
            "v0_1_62": (
                "SCAFFOLDED (still) -- BrokerEquityAdapter Protocol + "
                "NullBrokerEquityAdapter + make_poller_for factory "
                "added + 24 tests. Contract layer formalised. Runtime "
                "still did not call reconcile()."
            ),
            "v0_1_63": (
                "CLOSED (observation-only) -- ApexRuntime wires and "
                "drives the reconciler; alert on transition; full "
                "classification in tick log. KillVerdict synthesis "
                "deferred to v0.2.x by design, not by omission."
            ),
        },
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_added_in_this_bundle": R1_TESTS_ADDED,
        "tests_delta_residual_from_other_modules": (NEW_TESTS_ABS - prev_tests - R1_TESTS_ADDED),
        "ruff_green_touched_files": True,
        "full_suite_runtime_seconds": 126.75,
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "R1 end-to-end runtime wiring. ApexRuntime now "
                    "starts/stops the BrokerEquityPoller, feeds "
                    "tier-A equity through the reconciler each "
                    "tick, logs every classification, alerts on "
                    "transition into broker_below_logical. R1 "
                    "flipped SCAFFOLDED -> CLOSED (observation-only)."
                ),
                "tests_delta": R1_TESTS_ADDED,
                "tests_passing": NEW_TESTS_ABS,
            },
        )

    state["overall_progress_pct"] = state.get("overall_progress_pct", 100)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {VERSION} at {now}")
    print(
        f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} "
        f"({NEW_TESTS_ABS - prev_tests:+d} full, "
        f"{R1_TESTS_ADDED:+d} in-scope)",
    )


if __name__ == "__main__":
    main()
