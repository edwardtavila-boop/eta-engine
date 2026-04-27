"""Open-testing harness tests -- P12_POLISH.open_testing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from eta_engine.brain.regime import RegimeType
from eta_engine.core.parameter_sweep import CellScore, Gate
from eta_engine.tests.harness_open import (
    pareto_frontier_for,
    pick_winner_from_results,
    run_forward_test_comparator,
    run_parameter_sweep,
    run_regime_slice_evaluator,
    summarize_sweep,
)

_Scorer = Callable[[Mapping[str, Any]], CellScore]

# ---------------------------------------------------------------------------
# run_parameter_sweep
# ---------------------------------------------------------------------------


def _constant_scorer(
    exp: float,
    dd: float = 5.0,
    wr: float = 0.55,
    trades: int = 100,
) -> _Scorer:
    def scorer(params: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=exp,
            max_dd_pct=dd,
            win_rate=wr,
            n_trades=trades,
        )

    return scorer


def test_run_parameter_sweep_evaluates_full_grid() -> None:
    ranges = {"conf": [4.5, 5.0, 5.5], "risk": [0.005, 0.010]}
    results = run_parameter_sweep(ranges, _constant_scorer(0.40))
    assert len(results) == 6


def test_run_parameter_sweep_returns_dicts_with_expected_keys() -> None:
    ranges = {"a": [1, 2]}
    results = run_parameter_sweep(ranges, _constant_scorer(0.40))
    keys = {"params", "expectancy_r", "max_dd_pct", "win_rate", "n_trades", "stability", "gate_pass"}
    assert keys.issubset(results[0].keys())


def test_run_parameter_sweep_orders_results_best_first() -> None:
    def scorer(params: Mapping[str, Any]) -> CellScore:
        # Higher conf => higher expectancy
        return CellScore(
            expectancy_r=0.1 * params["conf"],
            max_dd_pct=5.0,
            win_rate=0.55,
            n_trades=100,
        )

    ranges = {"conf": [1, 2, 3, 4, 5]}
    results = run_parameter_sweep(ranges, scorer)
    assert results[0]["params"]["conf"] == 5
    assert results[-1]["params"]["conf"] == 1


def test_run_parameter_sweep_respects_custom_gate() -> None:
    ranges = {"x": [1, 2]}
    # Only exp >= 0.50 passes
    gate = Gate(min_expectancy_r=0.50, max_dd_pct=100.0, min_trades=0)
    results = run_parameter_sweep(ranges, _constant_scorer(0.40), gate=gate)
    assert all(r["gate_pass"] is False for r in results)

    results2 = run_parameter_sweep(ranges, _constant_scorer(0.60), gate=gate)
    assert all(r["gate_pass"] is True for r in results2)


def test_run_parameter_sweep_empty_ranges_returns_empty_list() -> None:
    assert run_parameter_sweep({}, _constant_scorer(0.4)) == []


# ---------------------------------------------------------------------------
# pareto_frontier_for
# ---------------------------------------------------------------------------


def test_pareto_frontier_for_returns_non_dominated_cells() -> None:
    # 3 cells: A dominates B on everything, C is dominated by A
    def scorer(p: Mapping[str, Any]) -> CellScore:
        if p["x"] == "a":
            return CellScore(
                expectancy_r=0.6,
                max_dd_pct=5.0,
                win_rate=0.5,
                n_trades=50,
            )
        if p["x"] == "b":
            return CellScore(
                expectancy_r=0.3,
                max_dd_pct=10.0,
                win_rate=0.5,
                n_trades=50,
            )  # dominated
        return CellScore(
            expectancy_r=0.5,
            max_dd_pct=3.0,
            win_rate=0.5,
            n_trades=50,
        )  # non-dominated (better dd)

    ranges = {"x": ["a", "b", "c"]}
    frontier = pareto_frontier_for(ranges, scorer)
    xs = {f["params"]["x"] for f in frontier}
    assert "b" not in xs
    assert "a" in xs and "c" in xs


def test_pareto_frontier_for_empty_ranges() -> None:
    assert pareto_frontier_for({}, _constant_scorer(0.4)) == []


# ---------------------------------------------------------------------------
# run_forward_test_comparator
# ---------------------------------------------------------------------------


def test_forward_test_comparator_picks_higher_expectancy_winner() -> None:
    def scorer_a(p: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=0.50,
            max_dd_pct=5.0,
            win_rate=0.55,
            n_trades=100,
        )

    def scorer_b(p: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=0.30,
            max_dd_pct=5.0,
            win_rate=0.50,
            n_trades=100,
        )

    out = run_forward_test_comparator(scorer_a, scorer_b, {"conf": 5.5})
    assert out["winner"] == "bot_a"
    assert out["edge_r"] == 0.20
    assert out["edge_pct"] > 0


def test_forward_test_comparator_reports_tie_on_equal_expectancy() -> None:
    def s(p: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=0.40,
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=100,
        )

    out = run_forward_test_comparator(s, s, {"x": 1})
    assert out["winner"] == "tie"
    assert out["edge_r"] == 0.0


def test_forward_test_comparator_edge_pct_on_near_zero_does_not_divide_by_zero() -> None:
    def s_a(p: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=0.0,
            max_dd_pct=0.0,
            win_rate=0.5,
            n_trades=0,
        )

    def s_b(p: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=0.0001,
            max_dd_pct=0.0,
            win_rate=0.5,
            n_trades=0,
        )

    out = run_forward_test_comparator(s_a, s_b, {"x": 1})
    assert out["edge_pct"] >= 0.0  # finite, no division error


# ---------------------------------------------------------------------------
# run_regime_slice_evaluator
# ---------------------------------------------------------------------------


def test_regime_slice_evaluator_groups_bars_by_regime() -> None:
    bars = [{"c": 1.0}, {"c": 2.0}, {"c": 3.0}, {"c": 4.0}]
    regimes = [
        RegimeType.TRENDING,
        RegimeType.TRENDING,
        RegimeType.RANGING,
        RegimeType.TRENDING,
    ]

    seen: dict[str, int] = {}

    def scorer(p: Mapping[str, Any]) -> CellScore:
        seen[p["regime"]] = len(p["bars"])
        return CellScore(
            expectancy_r=0.4,
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=10,
        )

    out = run_regime_slice_evaluator(scorer, bars, regimes)
    assert seen == {"TRENDING": 3, "RANGING": 1}
    assert out["TRENDING"]["num_bars"] == 3.0
    assert out["RANGING"]["num_bars"] == 1.0


def test_regime_slice_evaluator_raises_on_length_mismatch() -> None:
    with pytest.raises(ValueError, match="must match"):
        run_regime_slice_evaluator(
            _constant_scorer(0.4),
            data=[{"c": 1.0}, {"c": 2.0}],
            regimes=[RegimeType.TRENDING],
        )


def test_regime_slice_evaluator_forwards_params_to_scorer() -> None:
    bars = [{"c": 1.0}]
    regimes = [RegimeType.HIGH_VOL]
    captured: dict[str, Any] = {}

    def scorer(p: Mapping[str, Any]) -> CellScore:
        captured.update(p)
        return CellScore(
            expectancy_r=0.4,
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=10,
        )

    run_regime_slice_evaluator(scorer, bars, regimes, params={"risk": 0.01})
    assert captured["risk"] == 0.01
    assert captured["regime"] == "HIGH_VOL"
    assert captured["bars"] == bars


# ---------------------------------------------------------------------------
# summarize_sweep / pick_winner_from_results
# ---------------------------------------------------------------------------


def test_summarize_sweep_handles_empty() -> None:
    s = summarize_sweep([])
    assert s["n_cells"] == 0
    assert s["winner"] is None


def test_summarize_sweep_reports_pass_count_and_stats() -> None:
    results = [
        {
            "params": {"x": 1},
            "expectancy_r": 0.50,
            "gate_pass": True,
            "max_dd_pct": 5.0,
            "win_rate": 0.5,
            "n_trades": 100,
            "stability": 0.0,
        },
        {
            "params": {"x": 2},
            "expectancy_r": 0.40,
            "gate_pass": True,
            "max_dd_pct": 5.0,
            "win_rate": 0.5,
            "n_trades": 100,
            "stability": 0.0,
        },
        {
            "params": {"x": 3},
            "expectancy_r": 0.10,
            "gate_pass": False,
            "max_dd_pct": 5.0,
            "win_rate": 0.5,
            "n_trades": 100,
            "stability": 0.0,
        },
    ]
    s = summarize_sweep(results)
    assert s["n_cells"] == 3
    assert s["n_pass"] == 2
    assert s["best_expectancy_r"] == 0.50
    assert s["winner"]["params"] == {"x": 1}


def test_pick_winner_from_results_returns_first_when_non_empty() -> None:
    r = [{"params": {"x": 1}}, {"params": {"x": 2}}]
    assert pick_winner_from_results(r) == {"params": {"x": 1}}


def test_pick_winner_from_results_returns_none_on_empty() -> None:
    assert pick_winner_from_results([]) is None


# ---------------------------------------------------------------------------
# End-to-end: sweep + regime slicing on realistic-shaped inputs
# ---------------------------------------------------------------------------


def test_harness_end_to_end_realistic_bot_surface() -> None:
    """Simulate how a bot would call the harness:
    1. parameter sweep to find best (conf, risk)
    2. regime slice the winner
    3. summary for dashboard
    """

    def bot_scorer(p: Mapping[str, Any]) -> CellScore:
        conf = p.get("conf", 5.0)
        risk = p.get("risk", 0.01)
        # Higher conf -> higher expectancy; higher risk -> higher dd
        return CellScore(
            expectancy_r=(conf - 4.0) * 0.10,
            max_dd_pct=risk * 1000.0,
            win_rate=0.50 + (conf - 4.0) * 0.02,
            n_trades=int(200 / conf),
        )

    sweep = run_parameter_sweep(
        param_ranges={"conf": [5.0, 6.0, 7.0], "risk": [0.005, 0.010]},
        scorer=bot_scorer,
    )
    summary = summarize_sweep(sweep)
    assert summary["n_cells"] == 6
    winner = pick_winner_from_results(sweep)
    assert winner is not None
    # Best expectancy is conf=7
    assert winner["params"]["conf"] == 7.0

    regimes = [
        RegimeType.TRENDING,
        RegimeType.TRENDING,
        RegimeType.RANGING,
        RegimeType.HIGH_VOL,
    ]
    bars = [{"close": 100.0 + i} for i in range(4)]
    sliced = run_regime_slice_evaluator(bot_scorer, bars, regimes, params={"conf": 7.0})
    assert set(sliced.keys()) == {"TRENDING", "RANGING", "HIGH_VOL"}
