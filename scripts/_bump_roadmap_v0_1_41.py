"""One-shot: bump roadmap_state.json to v0.1.41.

PER-STRATEGY OOS QUALIFIER -- walk-forward + DSR gate runs against
the strategies backtest harness so every AI-Optimized strategy earns
its runtime allowlist slot per asset.

Context
-------
v0.1.36 shipped ``strategies.backtest_harness.run_harness`` -- a
lightweight bar-replay that scores every AI-Optimized strategy on a
tape and emits per-strategy R-multiple stats. v0.1.38 wired the
decision sink so dispatch decisions are audit-captured. v0.1.40 made
portfolio rebalance sweeps executable. The gap that remained: nothing
converted the harness's backtest report into a *keep-or-kill verdict*
per strategy per asset. Strategies were entering dispatch purely on
DEFAULT_ELIGIBILITY, with no OOS hurdle.

v0.1.41 closes the qualification loop. A new
``strategies.oos_qualifier`` module walks the harness through
rolling IS/OOS windows, computes a per-strategy Sharpe-like statistic
from the R-multiple distribution, applies the Deflated Sharpe Ratio
with n_trials = number of windows, and grades every strategy against
a three-condition gate (DSR, degradation, min trades).

What v0.1.41 adds
-----------------
  * ``strategies/oos_qualifier.py`` (new, ~360 lines)

    - ``QualificationGate`` frozen dataclass -- ``dsr_threshold=0.5``,
      ``max_degradation_pct=0.35``, ``min_trades_per_window=20``.
      ``DEFAULT_QUALIFICATION_GATE`` exposes the canonical instance.
    - ``PerStrategyWindow`` frozen dataclass -- one row per (strategy,
      window) with is/oos trade counts, Sharpe-like, total R, hit rate,
      degradation, min-trades flag.
    - ``StrategyQualification`` frozen dataclass -- cross-window
      verdict per strategy: avg is/oos Sharpe, avg degradation, DSR on
      pooled OOS trade R-distribution, pass/fail + reason list.
    - ``QualificationReport`` frozen dataclass -- top-level report with
      ``passing_strategies`` / ``failing_strategies`` helpers and a
      full ``as_dict`` for dashboard consumption.
    - ``qualify_strategies(bars, asset, *, gate, n_windows=4,
      is_fraction=0.7, ctx_builder, harness_config, eligibility,
      registry) -> QualificationReport`` -- the entry point.

  * ``tests/test_strategies_oos_qualifier.py`` (new, +40 tests)

    Ten test classes:
      - ``TestQualificationGateDefaults`` -- 0.5 DSR / 35% deg / 20
        trades + frozen invariant + custom override. 5
      - ``TestSharpeLike`` -- empty, single, identical, positive mean,
        negative mean, symmetric. 6
      - ``TestMoments`` -- empty + single default to (0, 3);
        symmetric near-zero skew; right-tail positive skew. 4
      - ``TestDegradation`` -- equal Sharpes, OOS-better clipped,
        50%-degraded, zero-IS-SR paths. 5
      - ``TestBuildWindows`` -- empty tape, below warmup, normal case,
        non-overlap, boundary fraction, zero-windows guard. 6
      - ``TestQualifyStrategiesInsufficientBars`` -- empty + below
        warmup paths yield notes=("insufficient_bars_no_windows",). 2
      - ``TestQualifyStrategiesHappyPath`` -- winning tape passes
        permissive gate; IS/OOS trade counts populated. 2
      - ``TestQualifyStrategiesFailurePaths`` -- losing tape fails DSR;
        impossible min_trades fails min_trades. 2
      - ``TestPerWindowRecords`` -- per-window rows unique per (sid,
        window_id); window_id monotonic within strategy. 2
      - ``TestReportSerialisation`` -- as_dict keys; PerStrategyWindow
        + StrategyQualification as_dict; passing/failing helpers cover
        qualifications exactly. 4
      - ``TestMultiStrategy`` -- router picks FVG over LSD on
        confidence; asset upper-cased. 2

Delta
-----
  * tests_passing: 1885 -> 1925 (+40 new oos qualifier tests)
  * All pre-existing tests still pass unchanged
  * Ruff-clean on the new module and test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.41 a strategy earned its slot in the live router's
dispatch table purely through DEFAULT_ELIGIBILITY (a hand-curated per-
asset list) with no OOS verification. That meant a strategy that
curve-fit to the IS tape would keep firing in production until a
human noticed the edge had rotted.

With the qualifier in place the pipeline is:

    1. Run ``qualify_strategies(bars_for_asset, asset)``.
    2. Intersect ``report.passing_strategies`` with
       DEFAULT_ELIGIBILITY[asset] -> runtime allowlist.
    3. Any strategy that fails the DSR, degradation, or min-trades
       gate is excluded from dispatch until re-qualified.

The policy router already accepts a custom eligibility map via
``dispatch(... eligibility=...)``, so the next live wire-up (v0.1.42)
becomes a trivial intersection + cache refresh.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.41"
NEW_TESTS_ABS = 1925


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_41_oos_qualifier"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "PER-STRATEGY OOS QUALIFIER -- walk-forward + DSR gate "
            "runs the harness through IS/OOS windows to decide "
            "keep-or-kill per strategy per asset"
        ),
        "theme": (
            "Compose the backtest harness with the Deflated Sharpe "
            "Ratio on a rolling walk-forward grid. Every AI-Optimized "
            "strategy now earns (or fails) its runtime allowlist slot "
            "via an explicit OOS hurdle: DSR > 0.5, avg degradation "
            "< 35%, min trades per window cleared in every window."
        ),
        "artifacts_added": {
            "strategies": ["strategies/oos_qualifier.py"],
            "tests": ["tests/test_strategies_oos_qualifier.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_41.py"],
        },
        "api_surface": {
            "QualificationGate": (
                "(dsr_threshold=0.5, max_degradation_pct=0.35, min_trades_per_window=20)  -- frozen dataclass."
            ),
            "DEFAULT_QUALIFICATION_GATE": "QualificationGate()",
            "PerStrategyWindow": (
                "strategy, window_id, is_n_trades, is_sharpe_like, "
                "is_total_r, is_hit_rate, oos_n_trades, "
                "oos_sharpe_like, oos_total_r, oos_hit_rate, "
                "degradation_pct, min_trades_met"
            ),
            "StrategyQualification": (
                "strategy, asset, n_windows, avg_is_sharpe, "
                "avg_oos_sharpe, avg_degradation_pct, dsr, "
                "n_trades_is_total, n_trades_oos_total, passes_gate, "
                "fail_reasons"
            ),
            "QualificationReport": (
                "asset, gate, n_windows_requested, n_windows_executed, "
                "per_window, qualifications, notes, "
                "passing_strategies, failing_strategies, as_dict()"
            ),
            "qualify_strategies": (
                "(bars, asset, *, gate, n_windows=4, is_fraction=0.7, "
                "ctx_builder, harness_config, eligibility, registry) "
                "-> QualificationReport"
            ),
        },
        "design_notes": {
            "sharpe_like_per_trade": (
                "Per-trade mean / stddev of R-multiples -- scale- invariant and matches DSR's input contract directly."
            ),
            "dsr_n_trials_equals_windows": (
                "Each window is an independent backtest trial; setting "
                "n_trials to the executed-window count is the Gumbel "
                "correction Lopez de Prado prescribes. More windows "
                "deflate more aggressively."
            ),
            "gate_condition_lattice": (
                "All three gate conditions must hold: DSR > threshold "
                "AND avg_degradation < max AND min_trades_per_window "
                "met in EVERY window. Explicit fail_reasons list makes "
                "the failing condition inspectable without re-running."
            ),
            "non_anchored_rolling_windows": (
                "Windows are contiguous non-overlapping slices of the "
                "usable span. Avoids cross-window trade reuse so each "
                "window contributes independent OOS evidence."
            ),
            "router_integration_ready": (
                "Output is already shaped for integration: "
                "report.passing_strategies intersected with "
                "DEFAULT_ELIGIBILITY[asset] produces the runtime "
                "allowlist the router's `eligibility=` parameter "
                "already accepts."
            ),
        },
        "test_coverage": {
            "tests_added": 40,
            "classes": {
                "TestQualificationGateDefaults": 5,
                "TestSharpeLike": 6,
                "TestMoments": 4,
                "TestDegradation": 5,
                "TestBuildWindows": 6,
                "TestQualifyStrategiesInsufficientBars": 2,
                "TestQualifyStrategiesHappyPath": 2,
                "TestQualifyStrategiesFailurePaths": 2,
                "TestPerWindowRecords": 2,
                "TestReportSerialisation": 4,
                "TestMultiStrategy": 2,
            },
        },
        "ruff_clean_on": [
            "strategies/oos_qualifier.py",
            "tests/test_strategies_oos_qualifier.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; the "
                "qualifier gives the live policy router an objective "
                "OOS test to run before any new strategy variant earns "
                "live capital. Curve-fit edges now have to survive a "
                "Deflated Sharpe gate before they reach dispatch."
            ),
            "note": (
                "v0.1.42 will plug the qualifier output into the live "
                "router's eligibility parameter as a cached intersect: "
                "passing_strategies & DEFAULT_ELIGIBILITY[asset] -> "
                "runtime allowlist, refreshed on a cadence."
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
                    "Per-strategy OOS qualifier composes the backtest "
                    "harness with rolling walk-forward windows + DSR "
                    "gate: every AI-Optimized strategy now earns its "
                    "runtime allowlist slot via an explicit OOS "
                    "hurdle (DSR>0.5, deg<35%, 20+ trades/window)."
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
        "  shipped: strategies/oos_qualifier.py + 40 tests. "
        "Walk-forward + DSR gate is live on the harness; "
        "qualify_strategies returns a per-asset pass/fail verdict."
    )


if __name__ == "__main__":
    main()
