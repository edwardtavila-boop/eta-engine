"""One-shot: bump roadmap_state.json to v0.1.40.

REBALANCE EXECUTION CHANNEL -- RebalancePlan sweeps route through the
funnel orchestrator as real TransferRequests.

Context
-------
v0.1.39 shipped ``strategies.portfolio_rebalancer.plan_rebalance``: the
pure function that converts regime_allocator weights + FunnelSnapshot
into a :class:`RebalancePlan` (a tuple of :class:`ProposedSweep`). But
nothing was actually *executing* those sweeps -- they were inert
proposals. v0.1.40 closes that gap. The orchestrator gains a dedicated
``execute_rebalance`` channel that turns every proposed sweep into a
:class:`TransferRequest` and routes it through the same executor that
already handles profit sweeps.

What v0.1.40 adds
-----------------
  * ``strategies/portfolio_rebalancer.py`` -- new pure helper
    ``rebalance_plan_to_transfers(plan, layer_to_bot) ->
    list[TransferRequest]``. Maps :class:`LayerId` -> bot-name strings.
    Unmapped sweeps are silently skipped so operators can intentionally
    narrow the rebalance surface (e.g. disable staking moves while a
    validator is unbonding). Length diff versus ``plan.sweeps`` tells
    the caller how many were dropped.

  * ``funnel/orchestrator.py`` -- new ``async execute_rebalance(
    rebalance_plan, layer_to_bot) -> list[TransferResult]`` method on
    :class:`FunnelOrchestrator`. Reuses the existing ``transfer_executor``
    injection point, so a ``TransferManager`` wired in front (with its
    policy + whitelist + daily limits + approval gate) governs the
    rebalance channel for free. Also:
      - Drops pre-existing ``ANN401`` noise by replacing ``allocator:
        Any`` with a concrete ``AllocatorFn = Callable[[float],
        dict[str, float]]``.
      - Drops pre-existing ``TC001`` noise by moving ``EquityMonitor``
        into the TYPE_CHECKING block.

  * ``tests/test_funnel_rebalance_execution.py`` (new, +26 tests)

    Four test classes:
      - ``TestRebalancePlanToTransfers`` -- pure converter invariants:
        empty plan, single sweep, layer->bot mapping, amount rounding,
        reason propagation, requires_approval default, missing source,
        missing destination, order preservation, gap detection via
        length diff, multiple sweeps from same layer. 11
      - ``TestOrchestratorExecuteRebalance`` -- empty plan, happy path,
        mapped bot names on executor call, result count match,
        partial-map skip, StubExecutor EXECUTED, TransferManager
        whitelist rejection, TransferManager approval-gate rejection,
        DryRunExecutor APPROVED. 9
      - ``TestRebalanceExecutionEndToEnd`` -- plan_rebalance output
        through orchestrator (drifted snap), on-plan no-op, kill-switch
        plan no-op, partial layer map reachable-only, zero equity no-op.
        5
      - ``TestExecutorInjection`` -- raw async callable (not a protocol
        instance) as transfer_executor works. 1

Delta
-----
  * tests_passing: 1859 -> 1885 (+26 new rebalance-execution tests)
  * All pre-existing tests still pass unchanged
  * Ruff-clean on the three edited modules
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.40 the PORTFOLIO-tier strategy could see that the funnel
was drifting off plan, but it could not DO anything about it -- the
rebalancer emitted inert sweep proposals that no consumer was wired to.
v0.1.40 connects the last wire: every time ``plan_rebalance`` returns
non-empty sweeps, the orchestrator can now route them through the same
``TransferManager`` that governs profit sweeps. The policy gate, the
whitelist, the daily-volume limit, the manual-approval threshold --
all of it applies to rebalance transfers with zero additional code.
Portfolio allocation finally became operational.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.40"
NEW_TESTS_ABS = 1885


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_40_rebalance_execution_channel"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "REBALANCE EXECUTION CHANNEL -- RebalancePlan sweeps route "
            "through the funnel orchestrator as real TransferRequests"
        ),
        "theme": (
            "Close the last gap between regime-aware target weights and "
            "live capital flow. plan_rebalance produced sweep proposals "
            "in v0.1.39; v0.1.40 wires them through the orchestrator so "
            "they become concrete inter-layer transfers governed by the "
            "same TransferManager policy + whitelist + approval gate as "
            "profit sweeps."
        ),
        "artifacts_added": {
            "tests": ["tests/test_funnel_rebalance_execution.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_40.py"],
        },
        "artifacts_modified": {
            "strategies": [
                "strategies/portfolio_rebalancer.py (+rebalance_plan_to_transfers)",
            ],
            "funnel": [
                "funnel/orchestrator.py (+execute_rebalance, +AllocatorFn, TYPE_CHECKING tidy)",
            ],
        },
        "api_surface": {
            "rebalance_plan_to_transfers": (
                "(plan, layer_to_bot) -> list[TransferRequest]  -- pure "
                "converter. Unmapped sweeps are silently skipped; use "
                "len(result) vs len(plan.sweeps) for gap detection."
            ),
            "FunnelOrchestrator.execute_rebalance": (
                "async (rebalance_plan, layer_to_bot) -> "
                "list[TransferResult]  -- routes sweeps through the "
                "existing transfer_executor injection point."
            ),
            "AllocatorFn": (
                "Callable[[float], dict[str, float]]  -- replaces the "
                "prior `allocator: Any` in FunnelOrchestrator.__init__."
            ),
        },
        "design_notes": {
            "separate_from_profit_sweep_channel": (
                "execute_rebalance is NOT folded into tick()/execute_tick(). "
                "Profit sweeps are driven by bot-level equity crossing a "
                "threshold; rebalance sweeps are driven by funnel-level "
                "drift from target weights. Keeping them as distinct "
                "channels means operators can disable one without "
                "touching the other."
            ),
            "reuses_transfer_executor": (
                "The orchestrator's existing transfer_executor callable "
                "routes both flows. Wiring a TransferManager gives "
                "rebalance transfers the same policy + whitelist + "
                "daily-volume limit + approval gate as profit sweeps "
                "with zero additional code."
            ),
            "silent_skip_on_missing_mapping": (
                "Sweeps whose source or destination LayerId is missing "
                "from layer_to_bot are dropped silently. This is an "
                "intentional escape hatch: operators can narrow the "
                "rebalance surface (e.g. freeze staking moves during a "
                "validator-unbonding window) without editing the plan."
            ),
            "local_import_breaks_cycle": (
                "funnel.orchestrator imports funnel.transfer at module "
                "scope; strategies.portfolio_rebalancer also imports "
                "funnel.transfer. The rebalancer-helper import lives "
                "inside execute_rebalance to keep the static import "
                "graph acyclic without disturbing ruff TCH rules."
            ),
            "lint_debt_cleanup": (
                "Pre-existing ANN401 on `allocator: Any` and TC001 on "
                "`EquityMonitor` were resolved as a drive-by; the file "
                "is now strict-ruff clean under the TCH + ANN rule sets."
            ),
        },
        "test_coverage": {
            "tests_added": 26,
            "classes": {
                "TestRebalancePlanToTransfers": 11,
                "TestOrchestratorExecuteRebalance": 9,
                "TestRebalanceExecutionEndToEnd": 5,
                "TestExecutorInjection": 1,
            },
        },
        "ruff_clean_on": [
            "strategies/portfolio_rebalancer.py",
            "funnel/orchestrator.py",
            "tests/test_funnel_rebalance_execution.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; the "
                "rebalance execution channel makes PORTFOLIO-tier "
                "strategy #5 a first-class operational controller: "
                "target weights now produce real transfers, not just "
                "inert sweep proposals."
            ),
            "note": (
                "v0.1.41 will compose backtest_harness with "
                "WalkForwardEngine + DSR gate so every strategy carries "
                "a per-asset OOS verdict before it's allowed to "
                "contribute to the live policy router."
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
                    "Rebalance execution channel wired through "
                    "FunnelOrchestrator.execute_rebalance: RebalancePlan "
                    "sweeps become TransferRequests and route through "
                    "the same TransferManager policy gate as profit "
                    "sweeps. Portfolio allocation is now operational."
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
        "  shipped: rebalance_plan_to_transfers + "
        "FunnelOrchestrator.execute_rebalance + 26 tests. "
        "RebalancePlan sweeps now execute as real TransferRequests "
        "through the orchestrator."
    )


if __name__ == "__main__":
    main()
