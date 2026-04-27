"""One-shot: bump roadmap_state.json to v0.1.36.

BACKTEST HARNESS -- replay the 6 AI-Optimized strategies on historical tape.

Context
-------
v0.1.35 finished the live-wire rollout: every bot can now consume the
strategies package. Before any real capital flows through we need to
answer "does the strategy edge actually exist on historical tape?" at
the same confidence levels and eligibility table the live bots will
use. v0.1.36 ships the walk-forward-ready harness that answers this.

What v0.1.36 adds
-----------------
  * ``strategies/backtest_harness.py`` (new, ~400 lines)

    - ``HarnessConfig`` -- warmup_bars (default 200), max_bars_per_trade
      (default 48), slippage_bps (5), record_decisions (False).
    - ``run_harness(bars, asset, *, ctx_builder, config, eligibility,
      registry) -> BacktestReport`` -- replays bars through
      ``policy_router.dispatch``, tracks one hypothetical trade per
      strategy at a time, resolves stop/target/timeout exits, and
      returns per-strategy + portfolio stats.
    - Exit logic is PESSIMISTIC on same-bar stop-vs-target
      collisions (stop checked first). Timeout exits price at the
      final bar's close.
    - Pure replay -- no pandas, no feature pipeline, no venue. The
      harness is a thin wrapper around the strategies package so a
      walk-forward driver can snapshot configs window-by-window.
    - Default ``default_ctx_builder`` emits a permissive TREND regime
      context so cold runs don't crash on asset tapes we don't yet
      have a features pipeline for. Real calibration callers inject
      their own builder.
    - Fallback 2R target is constructed when a strategy omits an
      explicit ``target`` (e.g. rl_full_automation signals).

  * ``tests/test_strategies_backtest_harness.py`` (new, +23 tests)

    Eight test classes:
      - ``TestHarnessGuards`` -- empty/short/warmup-boundary tapes. 3
      - ``TestExitResolution`` -- long/short target/stop/timeout. 5
      - ``TestSlippageApplication`` -- 2R minus 2 * 10bps. 1
      - ``TestStatsAggregation`` -- per-strategy aggregation + max
        consecutive losses streak. 2
      - ``TestInjection`` -- ctx_builder pass-through, custom
        eligibility, flat winner. 3
      - ``TestEdgeCases`` -- zero-stop skip, zero-target fallback,
        record_decisions opt-in, default empty. 4
      - ``TestSerialisation`` -- as_dict shapes + report properties. 4
      - ``TestDefaultCtxBuilder`` -- permissive defaults. 1

Delta
-----
  * tests_passing: 1747 -> 1770 (+23 new harness tests)
  * Every pre-existing bot/strategy test still passes unchanged
  * Ruff-clean on the new harness module and the test file
  * No phase-level status changes (overall_progress_pct stays at 99)

Why this matters
----------------
Before v0.1.36 we had six AI-Optimized strategies and a router that
could pick winners, but no programmatic way to say "strategy X has
edge on BTC 1m at confidence >= 7" or "strategy Y degrades 40% in
RANGING regime." The harness is the first tool that produces those
answers. v0.1.38 will compose it with the existing
``WalkForwardEngine`` + ``DSR`` gate to graduate strategies from
"compiled" to "calibrated" state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"

VERSION = "v0.1.36"
NEW_TESTS_ABS = 1770


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    sa["eta_engine_tests_passing"] = NEW_TESTS_ABS

    sa["eta_engine_v0_1_36_backtest_harness"] = {
        "timestamp_utc": now,
        "version": VERSION,
        "bundle_name": (
            "BACKTEST HARNESS -- pure-replay driver for the 6 AI-Optimized strategies with per-strategy stats"
        ),
        "theme": (
            "First calibration tool for the strategies package. "
            "Replays historical tape through policy_router.dispatch "
            "and produces per-strategy hit_rate / avg_r / total_r so "
            "we can gate eligibility thresholds before any live run."
        ),
        "artifacts_added": {
            "strategies": ["strategies/backtest_harness.py"],
            "tests": ["tests/test_strategies_backtest_harness.py"],
            "scripts": ["scripts/_bump_roadmap_v0_1_36.py"],
        },
        "api_surface": {
            "HarnessConfig": ("warmup_bars=200, max_bars_per_trade=48, slippage_bps=5.0, record_decisions=False"),
            "run_harness": (
                "(bars, asset, *, ctx_builder=None, config=None, eligibility=None, registry=None) -> BacktestReport"
            ),
            "BacktestReport": ("asset, total_bars, total_trades, trades, stats_by_strategy, decisions (opt-in)"),
            "StrategyBacktestStats": (
                "strategy, n_trades, hit_rate, avg_r, total_r, "
                "max_consecutive_losses, longest_trade_bars, "
                "avg_trade_bars"
            ),
            "ExitReason": "STOP | TARGET | TIMEOUT",
        },
        "design_notes": {
            "one_trade_per_strategy": (
                "A strategy cannot stack positions on itself. Different "
                "strategies can run concurrently within the same tape."
            ),
            "pessimistic_same_bar_resolution": (
                "When a bar's range touches both stop and target, STOP "
                "wins. This biases reported R downward which is the "
                "safer direction for a go/no-go calibration."
            ),
            "slippage_model": (
                "2 * slippage_bps / 10000 subtracted from raw R. "
                "Coarse but conservative; real slippage is budgeted in "
                "backtest.engine for live-replay fidelity."
            ),
            "fallback_target": (
                "rl_full_automation and a handful of regime variants "
                "return signals without explicit targets. The harness "
                "constructs a 2R fallback target so those trades still "
                "produce measurable R in the report."
            ),
        },
        "test_coverage": {
            "tests_added": 23,
            "classes": {
                "TestHarnessGuards": 3,
                "TestExitResolution": 5,
                "TestSlippageApplication": 1,
                "TestStatsAggregation": 2,
                "TestInjection": 3,
                "TestEdgeCases": 4,
                "TestSerialisation": 4,
                "TestDefaultCtxBuilder": 1,
            },
        },
        "ruff_clean_on": [
            "strategies/backtest_harness.py",
            "tests/test_strategies_backtest_harness.py",
        ],
        "phase_reconciliation": {
            "overall_progress_pct": 99,
            "status": (
                "unchanged -- still funding-gated on P9_ROLLOUT; this "
                "bundle unlocks empirical calibration of the strategy "
                "eligibility table before any live capital is routed."
            ),
            "note": (
                "v0.1.37 will wire RouterDecision into the existing "
                "decision-journal sink so live bot runs dump every "
                "dispatch for post-trade audit. v0.1.38 will compose "
                "this harness with WalkForwardEngine + DSR gate to "
                "formally qualify each strategy per-asset."
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
                    "Backtest harness for the 6 AI-Optimized strategies: "
                    "pure-replay driver with per-strategy hit_rate / "
                    "avg_r / consecutive-loss stats, ready to plug into "
                    "walk-forward calibration"
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
        "  shipped: strategies/backtest_harness.py + 23 tests. "
        "Calibration-ready replay driver for the 6 AI-Optimized "
        "strategies."
    )


if __name__ == "__main__":
    main()
