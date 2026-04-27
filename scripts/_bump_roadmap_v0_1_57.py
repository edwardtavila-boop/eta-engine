"""One-shot: bump roadmap_state.json to v0.1.57.

APEX EVAL READINESS CLOSURE (D-series) -- ships the five durable
safety modules the paper-run audit flagged as missing, wires them
into ``scripts/run_eta_live.py`` end-to-end, and runs the bootstrap
CI on the real-MNQ OOS R-distribution to anchor expectancy claims
in a confidence interval instead of a point estimate.

Why this bundle exists
----------------------
The v0.1.52-55 wave shipped the alpha-expansion core, adversarial
integrity kit, and chaos-drill closure, all of which landed in
isolation as library code. The operator's eval-readiness review
then flagged five open surfaces (labelled D1..D5) that had tests
or stubs but were never wired into the live tick-loop:

  * D1  session gate (Apex allowed-hours enforcement in ``MnqBot``)
  * D2  trailing-drawdown tick-snapshot tracker (Apex freeze rule
        enforced at tick granularity with durable state)
  * D3  30% consistency guard (largest-winning-day ratio tracked
        day-over-day with headroom alerts)
  * D4  end-of-day flatten signal (RouterAdapter surfacing into
        ``MnqBot.on_bar``)
  * D5  kill-switch latch (cold-boot gate + verdict recorder that
        survives runtime restarts)

A module that exists but is not wired into the runtime is not a
safety upgrade -- it is a promise. v0.1.57 cashes every promise
and adds integration tests that prove the tick-loop actually
reaches and uses each surface.

Bootstrap CI on real-MNQ OOS
----------------------------
Independently, the alpha-expansion bundle's headline expectancy
number (``+0.188R``) came from a point estimate over n=14 OOS
trades. A single point is not an edge -- so ``scripts/
bootstrap_ci_mnq_oos.py`` was run over 10,000 iterations (seed=11)
on the real ``.cache/parquet`` snapshot to produce a proper 95%
confidence interval. Verdict: ``NOISE``. The CI spans ``[-0.598,
+1.069]`` R -- a signed point estimate inside a very wide CI, so
the null hypothesis (no edge) cannot be rejected at n=14. This is
the honest reading; future OOS accumulation via paper run is the
path to narrowing the interval. The verdict now lives beside the
paper-run and walk-forward artifacts so no future bump can quote
expectancy without the CI.

What ships
----------
D1 ``session_gate`` wiring
  * ``eta_engine/bots/mnq/bot.py`` attaches ``SessionGate`` at
    init and forwards it to ``RouterAdapter``.
  * ``RouterAdapter`` now rejects entries outside the allowed
    Apex window and surfaces the reason on ``signal.meta`` so
    the journal records the gate hit.

D2 ``trailing_dd_tracker`` module + wiring
  * ``eta_engine/core/trailing_dd_tracker.py`` -- new module.
    Durable tick-granular tracker with the Apex freeze rule (once
    peak >= starting_balance + trailing_dd_cap, floor locks at
    starting_balance permanently). Atomic persistence (tmp +
    fsync + os.replace). Fail-closed on baseline mismatch.
  * ``scripts/run_eta_live.py`` now accepts a
    ``trailing_dd_tracker`` kwarg on ``ApexRuntime``. When
    attached, the tick loop sources ``ApexEvalSnapshot`` from
    the tracker (summing Tier-A equity). When absent the legacy
    path is preserved so progressive rollout is safe.

D3 ``consistency_guard`` module + wiring
  * ``eta_engine/core/consistency_guard.py`` -- new module.
    Per-day EoD history with a StrEnum status (``INSUFFICIENT_
    DATA / OK / WARNING / VIOLATION``) and headroom math that
    handles both regimes (prior_max_win already largest, and
    today-would-be-largest).
  * ``scripts/run_eta_live.py`` now accepts a
    ``consistency_guard`` kwarg on ``ApexRuntime``. The tick
    loop records today's tier-A session_realized_pnl into the
    guard, and emits a ``consistency_status`` alert + log entry
    exactly once on a WARNING/VIOLATION state transition.

D4 EoD flatten wiring
  * ``MnqBot.on_bar`` checks ``adapter.should_flatten_eod`` at
    every bar close; flatten reason ``apex_eod_flatten`` routes
    through the existing kill-switch dispatch path.

D5 ``kill_switch_latch`` wiring
  * ``KillSwitchLatch.boot_allowed()`` guards startup in
    ``run_eta_live`` -- a tripped latch blocks the loop from
    initializing.
  * ``KillSwitchLatch.record_verdict`` is invoked at every
    FLATTEN_* verdict so the latch state persists across
    restarts. Cold-boot after a real flatten requires operator
    un-latch.

Bootstrap CI artifact
  * ``scripts/bootstrap_ci_mnq_oos.py`` executed 10,000 iters
    (seed=11).
  * ``docs/cross_regime/bootstrap_ci_mnq5m_rth_oos.json`` -- raw
    numeric result (expectancy, sharpe, CI95, P(>0), trade
    count).
  * ``docs/cross_regime/bootstrap_ci_mnq5m_rth_oos.md`` -- human-
    readable note with the NOISE verdict and rationale.

Tests
  * ``tests/test_trailing_dd_tracker.py`` -- 30 tests (init,
    persistence, peak/floor, freeze rule, snapshot contract,
    breach counter, reset, KillSwitch compat).
  * ``tests/test_consistency_guard.py`` -- 32 tests (init,
    recording, status, headroom math, intraday path, reset,
    verdict serialization).
  * ``tests/test_run_eta_live.py`` -- 4 new async tests for D2
    tracker integration + 3 new async tests for D3 guard
    integration.
  * ``tests/test_kill_switch_latch.py`` and
    ``tests/test_session_gate.py`` extended during D1/D4/D5
    wiring.
  * Ruff-clean on every new file. Pre-existing ANN401/SIM105/
    TC003-Callable debt in ``run_eta_live.py`` is unrelated to
    this bump and is tracked separately.

Design choices
--------------
  * **Tracker attach is opt-in.** D2 preserves the legacy
    ``build_apex_eval_snapshot`` path when no tracker is
    attached, so the rollout can flip per-profile without
    touching every other code path.
  * **Corrupt state fails closed.** Both D2 and D3 raise on any
    load-time integrity violation (baseline mismatch, unparseable
    date key, NaN peak). A silent reset at runtime would hide
    the exact class of bug that Apex is strict about.
  * **State transition semantics for D3 alerts.** The guard
    stores the last-seen status and only emits an alert when the
    status *enters* WARNING/VIOLATION. Steady-state visibility
    is kept in tick logs without spamming the alert channel.
  * **Bootstrap CI verdict is the truth.** n=14 OOS trades
    produces a CI that straddles zero. The alpha-expansion
    bundle's point estimate (+0.188R) is kept in the roadmap
    but now carries the CI context so downstream consumers
    cannot read it as a validated edge.

Delta
-----
  * tests_passing: 3251 -> 3499 (+248)
    (covers D1/D4/D5 wiring tests, the two new D2/D3 suites,
    and the integration suite in ``test_run_eta_live.py``)
  * 2 new core modules, 2 new test modules, 1 bootstrap CI
    script invoked for the first time with durable artifacts
  * Ruff-clean on every new file and every modified new-file
    region
  * No overall_progress_pct change -- the roadmap was already
    at 100%; this bump is durable-safety plus OOS-edge honesty
    rather than new-scope expansion
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.57"
NEW_TESTS_ABS = 3499


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_57_apex_eval_readiness"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "APEX EVAL READINESS CLOSURE -- D1..D5 wired end-to-end "
            "in run_eta_live + MnqBot; bootstrap CI on real-MNQ OOS "
            "anchors expectancy in a confidence interval; NOISE "
            "verdict recorded."
        ),
        "theme": (
            "Every eval-readiness D-series surface flagged in the "
            "paper-run audit is now wired into the live tick-loop "
            "and covered by integration tests. Bootstrap CI on the "
            "real OOS R-distribution replaces the point-estimate "
            "headline with an honest CI that straddles zero at n=14."
        ),
        "operator_directive_quote": ("do all."),
        "modules_added": [
            "eta_engine/core/trailing_dd_tracker.py",
            "eta_engine/core/consistency_guard.py",
        ],
        "modules_modified": [
            "eta_engine/scripts/run_eta_live.py "
            "(trailing_dd_tracker kwarg + consistency_guard kwarg + "
            "tick-loop wiring + TYPE_CHECKING import hygiene)",
            "eta_engine/bots/mnq/bot.py (SessionGate attach + on_bar EoD flatten path)",
            "eta_engine/venues/router_adapter.py (session gate surface + should_flatten_eod signal)",
        ],
        "tests_added": [
            "eta_engine/tests/test_trailing_dd_tracker.py (30)",
            "eta_engine/tests/test_consistency_guard.py (32)",
        ],
        "tests_extended": [
            "eta_engine/tests/test_run_eta_live.py (TrailingDDTrackerIntegration + ConsistencyGuardIntegration)",
            "eta_engine/tests/test_kill_switch_latch.py",
            "eta_engine/tests/test_session_gate.py",
            "eta_engine/tests/test_mnq_bot.py (EoD flatten path)",
            "eta_engine/tests/test_router_adapter.py (session gate + should_flatten_eod)",
        ],
        "d_series_wiring": {
            "D1_session_gate": (
                "MnqBot.__init__ builds SessionGate from config; "
                "RouterAdapter receives the gate and rejects "
                "out-of-window entries with meta.gate_rejection."
            ),
            "D2_trailing_dd_tracker": (
                "ApexRuntime accepts trailing_dd_tracker kwarg. "
                "When attached, tick loop sums Tier-A equity, "
                "passes to tracker.update(), and uses the returned "
                "ApexEvalSnapshot in-place of build_apex_eval_snapshot. "
                "Legacy path preserved for rollout safety."
            ),
            "D3_consistency_guard": (
                "ApexRuntime accepts consistency_guard kwarg. Tick "
                "loop records today's tier-A session_realized_pnl "
                "via guard.record_eod; emits consistency_status alert "
                "+ log entry exactly once on WARNING/VIOLATION enter."
            ),
            "D4_eod_flatten": (
                "MnqBot.on_bar queries adapter.should_flatten_eod "
                "each bar close; routes reason=apex_eod_flatten "
                "through kill-switch dispatch."
            ),
            "D5_kill_switch_latch": (
                "run_eta_live calls KillSwitchLatch.boot_allowed() "
                "at startup -- refuses to init if tripped. "
                "record_verdict called at every FLATTEN_* verdict "
                "so latch state survives restarts. Cold-boot after "
                "a real flatten requires operator un-latch."
            ),
        },
        "bootstrap_ci_mnq_oos": {
            "script": "eta_engine/scripts/bootstrap_ci_mnq_oos.py",
            "iterations": 10000,
            "seed": 11,
            "n_bars_total": 5642,
            "n_bars_oos": 1693,
            "n_trades": 14,
            "expectancy_r_point": 0.1883,
            "sharpe_point": 1.7335,
            "ci95_low_r": -0.5980,
            "ci95_high_r": 1.0690,
            "p_gt_zero": 0.6695,
            "verdict": "NOISE",
            "artifact_json": ("eta_engine/docs/cross_regime/bootstrap_ci_mnq5m_rth_oos.json"),
            "artifact_md": ("eta_engine/docs/cross_regime/bootstrap_ci_mnq5m_rth_oos.md"),
            "note": (
                "CI95 straddles zero; n=14 too thin to reject null. "
                "Point estimate +0.188R is informative but not "
                "validated edge. Future OOS trades via paper-run "
                "accumulation is the path to narrower CI."
            ),
        },
        "design_choices": {
            "tracker_attach_is_opt_in": (
                "ApexRuntime constructs with tracker=None by default "
                "so existing callers keep the legacy path until the "
                "rollout flag flips."
            ),
            "corrupt_state_fails_closed": (
                "Both trackers raise on any load-time integrity "
                "violation (baseline mismatch, unparseable date key). "
                "Silent reset would hide exactly the class of bug "
                "Apex is strict about."
            ),
            "d3_alert_is_state_transition": (
                "Guard stores _last_consistency_status and only "
                "fires the alert when status *enters* WARNING/VIOLATION. "
                "Prevents spam while keeping steady-state visible in "
                "tick logs."
            ),
            "bootstrap_verdict_recorded_with_point_estimate": (
                "Alpha-expansion's +0.188R point estimate is kept "
                "alongside the CI verdict so downstream readers "
                "cannot mistake it for a validated edge."
            ),
            "type_checking_hygiene": (
                "TrailingDDTracker is imported under TYPE_CHECKING "
                "in run_eta_live because it only appears in "
                "annotations; ConsistencyGuard/Status/utc_today_iso "
                "stay at module scope because the guard's enum and "
                "helper are used at runtime."
            ),
        },
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_new": NEW_TESTS_ABS - prev_tests,
        "ruff_green_new_files": True,
        "pre_existing_lint_debt_in_run_eta_live": (
            "ANN401/ANN002/ANN003/SIM105/TC003-Callable errors in "
            "run_eta_live.py pre-date this bump and are tracked "
            "separately. Nothing new added."
        ),
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Apex Eval Readiness ships: D1 SessionGate, D2 "
                    "TrailingDDTracker, D3 ConsistencyGuard, D4 EoD "
                    "flatten, D5 KillSwitchLatch -- every surface "
                    "wired end-to-end in run_eta_live and covered "
                    "by integration tests. Bootstrap CI on real-MNQ "
                    "OOS recorded with NOISE verdict (n=14 too thin)."
                ),
                "tests_delta": NEW_TESTS_ABS - prev_tests,
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
        f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})",
    )


if __name__ == "__main__":
    main()
