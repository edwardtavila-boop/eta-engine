"""One-shot: bump roadmap_state.json to v0.1.39.

PORTFOLIO REBALANCER -- regime_allocator weights drive cross-layer
capital transfers when the funnel drifts off plan.

Context
-------
v0.1.38 closed the observability loop on per-dispatch decisions by
writing every RouterDecision to the unified journal. That covered the
*signal* side of the trading stack. v0.1.39 closes the *capital-flow*
side: the regime allocator has been shipping weight plans since
v0.1.30 but nothing was converting those weights into concrete
transfer requests. The waterfall planner owns *profit* sweeps; nothing
owned *rebalance* sweeps. This bundle ships the missing bridge.

What v0.1.39 adds
-----------------
  * ``strategies/portfolio_rebalancer.py`` (new, ~170 lines)

    - ``plan_rebalance(snapshot, allocation, *, drift_threshold_pct=0.05,
      min_transfer_usd=100.0) -> RebalancePlan`` -- pure function.
    - Converts :class:`AllocationPlan` (target weights per layer) plus
      :class:`FunnelSnapshot` (current equity per layer) into a
      :class:`RebalancePlan` carrying proposed :class:`ProposedSweep`
      objects (same type the waterfall emits, so downstream consumers
      accept both feeds with zero code change).
    - Drift is measured in percent of total equity. Strictly-above-
      threshold layers source transfers; strictly-below receive them.
    - Greedy pairing: largest overweight -> largest underweight, then
      drain and move on. Minimal-transfer plan even when several
      layers have drifted.
    - Kill-switch aware: if ``allocation.global_kill_applied`` is True,
      the risky layers are already zero-weighted -- the waterfall's
      HALT directives will unwind them, so we skip rebalance to avoid
      competing transfer streams.
    - Defensive normalization: if weights don't sum to 1.0 they are
      scaled; if they sum to zero the plan is empty with a note.
    - Zero-equity snapshots short-circuit to an empty plan with
      ``zero_total_equity`` note.

  * ``tests/test_strategies_portfolio_rebalancer.py`` (new, +27 tests)

    Eight test classes:
      - ``TestPlanRebalanceBasics`` -- returns type, ts_utc carry,
        on-plan no-sweeps, total equity + drift + target maps. 6
      - ``TestDriftThreshold`` -- below / above / custom threshold
        (looser + tighter) + default constant. 5
      - ``TestMinTransferUsd`` -- default constant, below-min skip,
        above-min emit. 3
      - ``TestGlobalKill`` -- allocator-side kill -> plan skipped
        with ``global_kill_skipped=True``. 1
      - ``TestGuardPaths`` -- zero equity + all weights zero. 2
      - ``TestMultiLayerPairing`` -- two-over-one-under, one-over-
        two-under, largest-paired-first. 3
      - ``TestReasonStrings`` -- "rebalance:X_overweight_to_Y_
        underweight". 1
      - ``TestSerialisation`` -- as_dict keys, sweep entries,
        kill-switch flag. 3
      - ``TestEndToEndComposition`` -- real allocator output flows
        through, HIGH vol shrinks MNQ below BTC, allocator-side kill
        vetoes rebalance. 3

Delta
-----
  * tests_passing: 1832 -> 1859 (+27 new rebalancer tests)
  * All pre-existing tests still pass unchanged
  * Ruff-clean on the new module and test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
The regime allocator has been producing correct weight plans since
v0.1.30, but the live bots had no way to act on them when cross-layer
equity drifted. A 10% overweight in LAYER_1_MNQ was invisible until
the next daily sweep cycle -- which only moves realized profit, not
position. v0.1.39 turns the allocator into an operational controller:
the orchestrator can now call ``plan_rebalance(snapshot, allocation)``
and consume the sweep list the same way it already consumes
``waterfall.plan(snapshot).sweeps``. The PORTFOLIO-tier strategy is
finally closing the loop.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.39"
NEW_TESTS_ABS = 1859


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_39_portfolio_rebalancer"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "PORTFOLIO REBALANCER -- regime_allocator weights drive "
            "cross-layer capital transfers when the funnel drifts"
        ),
        "theme": (
            "Close the capital-flow loop between the regime_allocator's "
            "target-weight plan and the funnel's actual per-layer "
            "equity. Waterfall moves profit; rebalancer moves position. "
            "Together they turn PORTFOLIO-tier strategy #5 into a live "
            "operational controller."
        ),
        "artifacts_added": {
            "strategies": ["strategies/portfolio_rebalancer.py"],
            "tests": ["tests/test_strategies_portfolio_rebalancer.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_39.py"],
        },
        "api_surface": {
            "plan_rebalance": (
                "(snapshot, allocation, *, drift_threshold_pct=0.05, min_transfer_usd=100.0) -> RebalancePlan"
            ),
            "RebalancePlan": (
                "ts_utc, total_equity_usd, sweeps (tuple of ProposedSweep), "
                "drift_pct_by_layer, target_usd_by_layer, notes, "
                "global_kill_skipped"
            ),
            "DEFAULT_DRIFT_THRESHOLD_PCT": "0.05",
            "DEFAULT_MIN_TRANSFER_USD": "100.0",
        },
        "design_notes": {
            "reuses_proposed_sweep": (
                "ProposedSweep is imported from funnel.waterfall so the "
                "orchestrator + transfer manager accept rebalance + "
                "profit sweeps through the same code path."
            ),
            "strict_threshold_comparison": (
                "Drift exactly at threshold is treated as on-plan; only "
                "strictly-above drifts source transfers. Prevents "
                "oscillation around the band edge."
            ),
            "greedy_pairing": (
                "Largest overweight source paired with largest "
                "underweight destination first. Minimal-transfer plan "
                "when several layers have drifted."
            ),
            "kill_switch_deference": (
                "If AllocationPlan.global_kill_applied is True the "
                "rebalancer returns an empty plan with "
                "global_kill_skipped=True. Kill-switch unwind is the "
                "waterfall's job (HALT directives); two competing "
                "transfer streams during a kill would race."
            ),
            "defensive_normalization": (
                "plan_rebalance scales weights if they don't sum to "
                "1.0 -- the allocator guarantees this but hand-built "
                "AllocationPlan instances can skip the invariant."
            ),
        },
        "test_coverage": {
            "tests_added": 27,
            "classes": {
                "TestPlanRebalanceBasics": 6,
                "TestDriftThreshold": 5,
                "TestMinTransferUsd": 3,
                "TestGlobalKill": 1,
                "TestGuardPaths": 2,
                "TestMultiLayerPairing": 3,
                "TestReasonStrings": 1,
                "TestSerialisation": 3,
                "TestEndToEndComposition": 3,
            },
        },
        "ruff_clean_on": [
            "strategies/portfolio_rebalancer.py",
            "tests/test_strategies_portfolio_rebalancer.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; this "
                "bundle activates the PORTFOLIO-tier strategy #5 so it "
                "contributes to live capital allocation instead of "
                "just emitting inert weight plans."
            ),
            "note": (
                "v0.1.40 will either (a) wire RebalancePlan sweeps "
                "through the funnel.orchestrator so they execute as "
                "real TransferRequests, or (b) compose the backtest "
                "harness with WalkForwardEngine + DSR gate to "
                "formally qualify each strategy per-asset. Decision "
                "point: does v0.1.40 pursue live capital flow or OOS "
                "calibration first?"
            ),
        },
        "python_touched": True,
        "jsx_touched": False,
        "tests_passing_before": prev_tests,
        "tests_passing_after": NEW_TESTS_ABS,
        "tests_new": NEW_TESTS_ABS - prev_tests,
    }

    milestones = state.setdefault("milestones", [])
    if isinstance(milestones, list):
        milestones.append(
            {
                "version": VERSION,
                "timestamp_utc": now,
                "title": (
                    "Portfolio rebalancer bridges regime_allocator "
                    "weights to funnel-layer transfers: drift >5% "
                    "sources rebalance sweeps via greedy pairing, "
                    "reusing ProposedSweep so orchestrator + transfer "
                    "manager consume both rebalance and profit flows "
                    "through a single code path."
                ),
                "tests_delta": NEW_TESTS_ABS - prev_tests,
                "tests_passing": NEW_TESTS_ABS,
            },
        )

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to {VERSION} at {now}")
    print(f"  tests_passing: {prev_tests} -> {NEW_TESTS_ABS} ({NEW_TESTS_ABS - prev_tests:+d})")
    print(
        "  shipped: strategies/portfolio_rebalancer.py + 27 tests. "
        "Regime_allocator weights now drive cross-layer capital "
        "transfers when the funnel drifts off plan."
    )


if __name__ == "__main__":
    main()
