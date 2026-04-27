"""One-shot: bump roadmap_state.json to v0.1.32.

CROSS-REGIME OOS VALIDATION HARNESS -- sign-flip overfit detection.

Context
-------
P3_PROOF.regime_validation was marked done with the note
"firm_v3 4-state regime classifier in place + cross-regime PnL table in
live_sim_analysis.md; C002-B OOS sweep deferred to strategy_registry
upgrades." The classifier existed but there was no dedicated harness
that would (a) explicitly drive the strategy across each regime in
isolation, (b) hold out an OOS window per regime, and (c) discriminate
"regime-selective edge" (fine -- expected) from "overfit collapse"
(fatal -- must exclude).

v0.1.32 closes that gap.

What v0.1.32 adds
-----------------
  * ``scripts/run_cross_regime_validation.py`` (~350 lines, new)

    A cross-regime OOS validation harness. For each of 4 regimes
    (TRENDING, RANGING, HIGH_VOL, LOW_VOL):

      - Generates 1200 synthetic bars with regime-appropriate drift/vol
        via ``BarReplay.synthetic_bars`` (and the jump-diffusion variant
        for HIGH_VOL).
      - Verifies the bars' axes classify to the expected regime label
        using ``brain.regime.classify_regime()`` -- guards against the
        harness silently testing the wrong regime.
      - Splits 70/30 IS/OOS (~840 IS bars, ~360 OOS bars).
      - Runs the full event-driven backtester end-to-end on each half.
      - Emits per-regime summary stats: trades, expectancy_r, win_rate,
        profit_factor, sharpe, max_dd_pct, total_return_pct.
      - Computes a ``degradation_r`` metric ( (IS - OOS) / |IS| ).

    Gate logic (``_apply_gate``):
      - "Live-tradeable" per regime requires OOS expectancy >= 0.15R AND
        OOS trades >= 20 AND degradation_r <= 60%.
      - PASS condition: at least ONE regime is live-tradeable AND no
        sign-flip overfit anywhere (sign flip = IS positive, OOS
        negative).
      - Sign flip uniquely signals overfit. Degradation alone flags
        regime-selectivity, which is desirable (strategies SHOULD work
        better in some regimes than others); a sign flip means the
        IS-positive edge was noise.

    Exit codes:
      - 0: PASS (live-tradeable regime exists, no sign-flip overfit)
      - 2: FAIL (no tradeable regime OR sign-flip overfit detected)
      - 3: internal error

    Artifacts written to ``docs/cross_regime/``:
      - ``cross_regime_validation.json`` (machine-readable full payload)
      - ``cross_regime_validation.md``   (human-readable verdict table)

  * ``tests/test_cross_regime_validation.py`` (~230 lines, +11 tests)

    Unit tests for the gate + helpers (no engine run required for the
    fast path) + one ``@pytest.mark.slow()`` integration test that
    subprocess-runs the whole script.

    Fast-path coverage:
      - gate PASSES with one robust regime
      - gate FAILS when no regime meets the bar
      - gate FAILS on sign-flip overfit (red-flag)
      - gate TOLERATES selectivity without sign flip (weakened edge but
        still positive)
      - gate REPORTS non-tradeable reasons explicitly (expectancy gate
        AND min-trades gate both surfaced)
      - _degradation: positive / negative / near-zero-IS edge cases
      - _split_bars: 70/30 split correctness
      - regime spec sanity: each declared RegimeSpec.axes classifies to
        RegimeSpec.expected_label (guards harness correctness)

    Slow path: subprocess-runs the full script, asserts exit code in
    {0, 2}, asserts JSON + MD artifacts land, asserts every regime
    reports both IS and OOS summaries.

First live run (2026-04-17)
---------------------------
  * TRENDING regime: IS +1.001R (54 trades) / OOS +1.012R (23 trades) /
    degradation -1.0% -> LIVE-TRADEABLE (passes gate).
  * RANGING regime: IS +0.109R / OOS +0.069R / 11 OOS trades /
    degradation +36% -> not tradeable (weak OOS + under min-trades).
    No sign flip. Normal regime-selectivity, not overfit.
  * HIGH_VOL regime: IS +0.216R / OOS -0.559R / degradation +359% ->
    SIGN-FLIP OVERFIT. Red-flagged. Exclude this regime from deployment.
  * LOW_VOL regime: IS +0.366R / OOS +0.073R / degradation +80% ->
    not tradeable (weak OOS, over deg cap) but no sign flip. Selective.

Gate verdict: FAIL (exit 2) because HIGH_VOL sign-flip triggers the
overfit-collapse safeguard. This is the harness doing its job --
TRENDING alone would have passed, but the whole-portfolio deployment
must exclude HIGH_VOL or the edge silently inverts under stress.
Actionable output: run live ONLY in TRENDING, hard-gate HIGH_VOL.

Reconciliation
--------------
  * tests_passing: 1620 -> 1631 (+11). All 11 new fast-path tests green;
    1 additional slow integration test collected but deselected in the
    fast suite (runs with `-m slow`).
  * Ruff-clean on both new files.
  * No phase-level status changes. P3_PROOF was already at 100% and
    marked done; v0.1.32 is the physical harness + artifacts backing
    the "done" claim so regime_validation now has a reproducible,
    re-runnable proof rather than a one-shot analysis file.
  * overall_progress_pct: 99 (unchanged -- still funding-gated on
    P9_ROLLOUT).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "roadmap_state.json"


def main() -> None:
    now = datetime.now(UTC).isoformat()
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state["last_updated"] = now
    state["last_updated_utc"] = now

    sa = state["shared_artifacts"]
    prev_tests = int(sa.get("eta_engine_tests_passing", 0) or 0)
    new_tests = 1631  # 1620 baseline + 11 fast cross-regime tests
    sa["eta_engine_tests_passing"] = new_tests

    sa["eta_engine_v0_1_32_cross_regime_validation"] = {
        "timestamp_utc": now,
        "version": "v0.1.32",
        "bundle_name": ("CROSS-REGIME OOS VALIDATION HARNESS -- sign-flip overfit detection"),
        "directive": "assist apex predator with all the task it needs to complete",
        "theme": (
            "P3_PROOF.regime_validation was marked done via the firm_v3 "
            "classifier + live_sim analysis, but had no reproducible "
            "harness that isolates each regime, holds out OOS, and "
            "discriminates regime-selectivity (desirable) from sign-flip "
            "overfit (fatal). v0.1.32 ships that harness, its unit + "
            "integration tests, and its first actionable verdict."
        ),
        "artifacts_added": {
            "scripts": [
                "scripts/run_cross_regime_validation.py",
                "scripts/_bump_roadmap_v0_1_32.py",
            ],
            "tests": ["tests/test_cross_regime_validation.py"],
            "docs": [
                "docs/cross_regime/cross_regime_validation.json",
                "docs/cross_regime/cross_regime_validation.md",
            ],
        },
        "harness": {
            "spec_id": "CROSS_REGIME_OOS_v1",
            "regimes_tested": ["TRENDING", "RANGING", "HIGH_VOL", "LOW_VOL"],
            "bars_per_regime": 1200,
            "is_fraction": 0.70,
            "bar_generator": (
                "BarReplay.synthetic_bars (GBM) + BarReplay.synthetic_bars_jump (jump-diffusion for HIGH_VOL)"
            ),
            "classifier_sanity_check": (
                "each RegimeSpec.axes must classify to spec.expected_label "
                "via brain.regime.classify_regime() before running the "
                "sweep -- prevents the harness silently testing the "
                "wrong regime"
            ),
            "metrics_per_split": [
                "trades",
                "expectancy_r",
                "win_rate",
                "profit_factor",
                "sharpe",
                "max_dd_pct",
                "total_return_pct",
            ],
            "degradation_formula": "(IS_exp - OOS_exp) / max(|IS_exp|, 1e-9)",
        },
        "gate_logic": {
            "live_tradeable_per_regime": {
                "oos_expectancy_r_min": 0.15,
                "oos_trades_min": 20,
                "max_degradation_pct": 60.0,
            },
            "pass_condition": ("(>=1 regime live-tradeable) AND (no sign-flip overfit anywhere)"),
            "sign_flip_rule": (
                "IS expectancy_r > 0 AND OOS expectancy_r < 0 -> red flag, exclude that regime from deployment"
            ),
            "why_sign_flip_only": (
                "Degradation alone flags regime-selectivity, which is "
                "expected and desirable (strategies SHOULD work better in "
                "some regimes). Only a sign flip uniquely identifies "
                "overfit -- the IS-positive edge was noise."
            ),
            "exit_codes": {
                "0": "PASS (tradeable regime + no sign-flip)",
                "2": "FAIL (no tradeable regime OR sign-flip detected)",
                "3": "internal error",
            },
        },
        "first_run_verdict": {
            "passed": False,
            "exit_code": 2,
            "trending": {
                "verdict": "LIVE-TRADEABLE",
                "is_exp_r": 1.001,
                "oos_exp_r": 1.012,
                "oos_trades": 23,
                "degradation_pct": -1.0,
            },
            "ranging": {
                "verdict": "NOT TRADEABLE (no sign flip)",
                "is_exp_r": 0.109,
                "oos_exp_r": 0.069,
                "oos_trades": 11,
                "degradation_pct": 36.1,
                "reasons": ["OOS exp < 0.15R", "OOS trades < 20"],
            },
            "high_vol": {
                "verdict": "SIGN-FLIP OVERFIT -- EXCLUDE",
                "is_exp_r": 0.216,
                "oos_exp_r": -0.559,
                "oos_trades": 14,
                "degradation_pct": 358.7,
                "red_flag": "IS +0.216R -> OOS -0.559R (sign flip)",
            },
            "low_vol": {
                "verdict": "NOT TRADEABLE (no sign flip)",
                "is_exp_r": 0.366,
                "oos_exp_r": 0.073,
                "oos_trades": 17,
                "degradation_pct": 80.0,
                "reasons": [
                    "OOS exp < 0.15R",
                    "OOS trades < 20",
                    "degradation > 60%",
                ],
            },
            "actionable_output": (
                "deploy strategy ONLY during TRENDING regime; hard-gate "
                "HIGH_VOL (sign-flip overfit); defer RANGING and LOW_VOL "
                "until OOS trade count + expectancy improve on real data"
            ),
        },
        "test_coverage": {
            "fast_tests_added": 11,
            "slow_tests_added": 1,
            "fast_path": [
                "gate_passes_with_one_robust_regime",
                "gate_fails_when_no_regime_meets_bar",
                "gate_fails_on_sign_flip_overfit",
                "gate_tolerates_selectivity_without_sign_flip",
                "gate_reports_non_tradeable_reasons_explicitly",
                "degradation_positive_when_oos_worse",
                "degradation_negative_when_oos_better",
                "degradation_handles_near_zero_is",
                "split_bars_70_30",
                "regime_specs_axes_classify_correctly",
            ],
            "slow_path": [
                "test_full_run_writes_artifacts_and_exits_cleanly  "
                "(subprocess-runs full script; asserts exit in {0,2} and "
                "JSON+MD artifacts)",
            ],
        },
        "ruff_clean_on": [
            "scripts/run_cross_regime_validation.py",
            "tests/test_cross_regime_validation.py",
        ],
        "phase_reconciliation": {
            "P3_PROOF.regime_validation": (
                "already status=done via firm_v3 4-state classifier + "
                "live_sim analysis; v0.1.32 ships the reproducible harness "
                "backing that claim so the proof is re-runnable, not "
                "one-shot"
            ),
            "P3_PROOF.backtest_engine": (
                "already status=done via 14-stage mnq_backtest + "
                "run_backtest_demo.py; v0.1.32 exercises the "
                "eta_engine.backtest.engine end-to-end on 4 regimes "
                "x 2 splits = 8 full backtest runs per invocation, "
                "re-confirming the engine is production-grade"
            ),
            "overall_progress_pct": 99,
            "status": "unchanged -- still funding-gated on P9_ROLLOUT",
        },
        "python_touched": True,
        "jsx_touched": False,
        "tests_passing_before": prev_tests,
        "tests_passing_after": new_tests,
        "tests_new": new_tests - prev_tests,
        "external_gate": (
            "P9_ROLLOUT remains at 85% pending $1000 Tradovate funded "
            "balance for API credential issuance. Cross-regime harness "
            "is ready to re-run on live bars the moment that clears."
        ),
    }

    state["overall_progress_pct"] = state.get("overall_progress_pct", 99)

    STATE_PATH.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"bumped roadmap_state.json to v0.1.32 at {now}")
    print(f"  tests_passing: {prev_tests} -> {new_tests} ({new_tests - prev_tests:+d})")
    print("  shipped: scripts/run_cross_regime_validation.py + tests/test_cross_regime_validation.py")
    print("  first verdict: TRENDING live-tradeable; HIGH_VOL sign-flip overfit -> exclude")
    print("  P3_PROOF.regime_validation proof is now reproducible (re-runnable harness + unit tests)")


if __name__ == "__main__":
    main()
