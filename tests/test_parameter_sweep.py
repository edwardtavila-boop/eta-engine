"""Parameter-sweep engine tests -- P12_POLISH.parameter_sweep."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from eta_engine.core.parameter_sweep import (
    CellScore,
    Gate,
    SweepCell,
    SweepGrid,
    SweepParam,
    _dominates,
    pareto_frontier,
    pick_winner,
    rank_cells,
    run_sweep,
    walk_forward_windows,
)

_Scorer = Callable[[Mapping[str, Any]], CellScore]

# ---------------------------------------------------------------------------
# SweepParam / SweepGrid
# ---------------------------------------------------------------------------


def test_sweep_param_rejects_empty_values() -> None:
    with pytest.raises(ValidationError):
        SweepParam(name="conf", values=[])


def test_sweep_param_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        SweepParam(name="conf", values=[4.5, 4.5, 5.0])


def test_sweep_param_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        SweepParam(name="", values=[1.0])


def test_sweep_grid_cardinality_is_product_of_axes() -> None:
    grid = SweepGrid(
        params=[
            SweepParam(name="conf", values=[4.5, 5.0, 5.5]),
            SweepParam(name="risk", values=[0.005, 0.01]),
            SweepParam(name="atr", values=[1.5, 2.0, 2.5, 3.0]),
        ],
    )
    assert grid.cardinality() == 3 * 2 * 4


def test_sweep_grid_iter_combinations_yields_all_combos_in_product_order() -> None:
    grid = SweepGrid(
        params=[
            SweepParam(name="a", values=[1, 2]),
            SweepParam(name="b", values=["x", "y"]),
        ],
    )
    combos = list(grid.iter_combinations())
    assert combos == [
        {"a": 1, "b": "x"},
        {"a": 1, "b": "y"},
        {"a": 2, "b": "x"},
        {"a": 2, "b": "y"},
    ]


def test_sweep_grid_rejects_no_params() -> None:
    with pytest.raises(ValidationError):
        SweepGrid(params=[])


# ---------------------------------------------------------------------------
# CellScore validation
# ---------------------------------------------------------------------------


def test_cell_score_rejects_negative_dd() -> None:
    with pytest.raises(ValidationError):
        CellScore(expectancy_r=0.5, max_dd_pct=-1.0, win_rate=0.5, n_trades=50)


def test_cell_score_rejects_win_rate_out_of_bounds() -> None:
    with pytest.raises(ValidationError):
        CellScore(expectancy_r=0.5, max_dd_pct=5.0, win_rate=1.5, n_trades=50)


def test_cell_score_rejects_negative_trades() -> None:
    with pytest.raises(ValidationError):
        CellScore(expectancy_r=0.5, max_dd_pct=5.0, win_rate=0.5, n_trades=-1)


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------


def test_gate_passes_when_all_thresholds_met() -> None:
    g = Gate()
    s = CellScore(expectancy_r=0.40, max_dd_pct=10.0, win_rate=0.55, n_trades=100)
    assert g.evaluate(s) is True


def test_gate_fails_on_low_expectancy() -> None:
    g = Gate()
    s = CellScore(expectancy_r=0.20, max_dd_pct=10.0, win_rate=0.55, n_trades=100)
    assert g.evaluate(s) is False


def test_gate_fails_on_high_drawdown() -> None:
    g = Gate()
    s = CellScore(expectancy_r=0.40, max_dd_pct=30.0, win_rate=0.55, n_trades=100)
    assert g.evaluate(s) is False


def test_gate_fails_on_undersampled_cell() -> None:
    g = Gate(min_trades=50)
    s = CellScore(expectancy_r=0.50, max_dd_pct=5.0, win_rate=0.6, n_trades=10)
    assert g.evaluate(s) is False


def test_gate_custom_min_win_rate() -> None:
    g = Gate(min_win_rate=0.55)
    s = CellScore(expectancy_r=0.40, max_dd_pct=10.0, win_rate=0.50, n_trades=100)
    assert g.evaluate(s) is False


# ---------------------------------------------------------------------------
# run_sweep
# ---------------------------------------------------------------------------


def _scorer_factory(recipe: dict[tuple, CellScore]) -> _Scorer:
    """Return a scorer that looks params up in ``recipe`` by sorted-items tuple."""

    def scorer(params: Mapping[str, Any]) -> CellScore:
        key = tuple(sorted(params.items()))
        return recipe[key]

    return scorer


def test_run_sweep_evaluates_every_combination() -> None:
    grid = SweepGrid(
        params=[
            SweepParam(name="a", values=[1, 2]),
            SweepParam(name="b", values=[10, 20]),
        ],
    )

    seen: list[dict] = []

    def scorer(params: Mapping[str, Any]) -> CellScore:
        seen.append(dict(params))
        return CellScore(
            expectancy_r=0.4,
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=50,
        )

    cells = run_sweep(grid, scorer)
    assert len(cells) == 4
    assert len(seen) == 4
    # Every combo visited
    assert {tuple(sorted(s.items())) for s in seen} == {
        ((("a", 1), ("b", 10))),
        ((("a", 1), ("b", 20))),
        ((("a", 2), ("b", 10))),
        ((("a", 2), ("b", 20))),
    }


def test_run_sweep_marks_gate_pass_per_cell() -> None:
    grid = SweepGrid(params=[SweepParam(name="x", values=[1, 2, 3])])
    recipe = {
        (("x", 1),): CellScore(expectancy_r=0.10, max_dd_pct=5.0, win_rate=0.5, n_trades=50),
        (("x", 2),): CellScore(expectancy_r=0.50, max_dd_pct=5.0, win_rate=0.5, n_trades=50),
        (("x", 3),): CellScore(expectancy_r=0.50, max_dd_pct=50.0, win_rate=0.5, n_trades=50),
    }
    cells = run_sweep(grid, _scorer_factory(recipe))
    assert [c.gate_pass for c in cells] == [False, True, False]


def test_run_sweep_computes_stability_from_walk_forward_scores() -> None:
    grid = SweepGrid(params=[SweepParam(name="x", values=[1])])

    def scorer(params: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=0.4,
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=50,
            walk_forward_scores=[0.4, 0.4, 0.4, 0.4],
        )

    cells = run_sweep(grid, scorer)
    assert cells[0].stability == 0.0


def test_run_sweep_stability_is_zero_when_fewer_than_two_windows() -> None:
    grid = SweepGrid(params=[SweepParam(name="x", values=[1])])

    def scorer(params: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=0.4,
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=50,
            walk_forward_scores=[0.4],  # only 1 window
        )

    cells = run_sweep(grid, scorer)
    assert cells[0].stability == 0.0


def test_run_sweep_higher_wf_variance_yields_higher_stability_number() -> None:
    grid = SweepGrid(params=[SweepParam(name="x", values=[1, 2])])
    recipe = {
        (("x", 1),): CellScore(
            expectancy_r=0.4,
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=50,
            walk_forward_scores=[0.4, 0.4, 0.4],
        ),
        (("x", 2),): CellScore(
            expectancy_r=0.4,
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=50,
            walk_forward_scores=[0.1, 0.4, 0.9],  # volatile
        ),
    }
    cells = run_sweep(grid, _scorer_factory(recipe))
    assert cells[0].stability < cells[1].stability


def test_run_sweep_is_deterministic_under_same_inputs() -> None:
    grid = SweepGrid(
        params=[
            SweepParam(name="a", values=[1, 2, 3]),
            SweepParam(name="b", values=[0.1, 0.2]),
        ],
    )

    def scorer(p: Mapping[str, Any]) -> CellScore:
        return CellScore(
            expectancy_r=p["a"] * 0.1 + p["b"],
            max_dd_pct=5.0,
            win_rate=0.5,
            n_trades=50,
        )

    c1 = run_sweep(grid, scorer)
    c2 = run_sweep(grid, scorer)
    assert [c.params for c in c1] == [c.params for c in c2]
    assert [c.score.expectancy_r for c in c1] == [c.score.expectancy_r for c in c2]


# ---------------------------------------------------------------------------
# rank_cells / pick_winner
# ---------------------------------------------------------------------------


def _mk_cell(
    *,
    exp: float,
    dd: float = 5.0,
    stab: float = 0.0,
    gate: bool = True,
    trades: int = 50,
    wr: float = 0.5,
    name: str = "c",
) -> SweepCell:
    return SweepCell(
        params={"name": name},
        score=CellScore(
            expectancy_r=exp,
            max_dd_pct=dd,
            win_rate=wr,
            n_trades=trades,
        ),
        gate_pass=gate,
        stability=stab,
    )


def test_rank_cells_puts_passers_before_failers() -> None:
    a = _mk_cell(exp=0.50, gate=False, name="a")
    b = _mk_cell(exp=0.31, gate=True, name="b")
    c = _mk_cell(exp=0.60, gate=False, name="c")
    d = _mk_cell(exp=0.35, gate=True, name="d")
    ranked = rank_cells([a, b, c, d])
    assert [r.params["name"] for r in ranked[:2]] == ["d", "b"]  # passers first
    assert set(r.params["name"] for r in ranked[2:]) == {"a", "c"}


def test_rank_cells_sorts_by_expectancy_desc_among_passers() -> None:
    cells = [
        _mk_cell(exp=0.35, name="low"),
        _mk_cell(exp=0.55, name="high"),
        _mk_cell(exp=0.45, name="mid"),
    ]
    ranked = rank_cells(cells)
    assert [r.params["name"] for r in ranked] == ["high", "mid", "low"]


def test_rank_cells_tie_breaks_on_dd_then_stability() -> None:
    a = _mk_cell(exp=0.40, dd=8.0, stab=0.10, name="a")
    b = _mk_cell(exp=0.40, dd=5.0, stab=0.30, name="b")  # better dd, worse stab
    c = _mk_cell(exp=0.40, dd=5.0, stab=0.05, name="c")  # best dd AND best stab
    ranked = rank_cells([a, b, c])
    assert [r.params["name"] for r in ranked] == ["c", "b", "a"]


def test_pick_winner_returns_none_on_empty() -> None:
    assert pick_winner([]) is None


def test_pick_winner_prefers_passing_cell_over_higher_expectancy_failer() -> None:
    passer = _mk_cell(exp=0.31, gate=True, name="passer")
    failer = _mk_cell(exp=0.90, gate=False, name="failer")
    assert pick_winner([failer, passer]).params["name"] == "passer"


def test_pick_winner_falls_back_to_closest_to_passing_when_none_pass() -> None:
    a = _mk_cell(exp=0.10, gate=False, name="a")
    b = _mk_cell(exp=0.25, gate=False, name="b")  # closest to passing
    c = _mk_cell(exp=0.05, gate=False, name="c")
    assert pick_winner([a, b, c]).params["name"] == "b"


# ---------------------------------------------------------------------------
# Pareto frontier
# ---------------------------------------------------------------------------


def test_dominates_requires_at_least_one_strict_inequality() -> None:
    a = _mk_cell(exp=0.40, dd=5.0, stab=0.1)
    b = _mk_cell(exp=0.40, dd=5.0, stab=0.1)
    assert _dominates(a, b) is False
    assert _dominates(b, a) is False


def test_dominates_detects_strict_pareto() -> None:
    better = _mk_cell(exp=0.50, dd=5.0, stab=0.1)
    worse = _mk_cell(exp=0.40, dd=5.0, stab=0.1)  # lower expectancy, same rest
    assert _dominates(better, worse) is True
    assert _dominates(worse, better) is False


def test_dominates_requires_improvement_on_all_dims_not_worse_anywhere() -> None:
    a = _mk_cell(exp=0.50, dd=10.0, stab=0.1, name="a")  # higher expectancy
    b = _mk_cell(exp=0.40, dd=5.0, stab=0.1, name="b")  # lower dd
    # Neither dominates
    assert _dominates(a, b) is False
    assert _dominates(b, a) is False


def test_pareto_frontier_keeps_non_dominated() -> None:
    dominated = _mk_cell(exp=0.30, dd=10.0, stab=0.2, name="dom")
    a = _mk_cell(exp=0.50, dd=8.0, stab=0.2, name="a")  # high exp, med dd
    b = _mk_cell(exp=0.40, dd=5.0, stab=0.2, name="b")  # low dd
    c = _mk_cell(exp=0.35, dd=6.0, stab=0.05, name="c")  # best stability
    frontier = pareto_frontier([dominated, a, b, c])
    names = {cell.params["name"] for cell in frontier}
    assert names == {"a", "b", "c"}


def test_pareto_frontier_handles_single_cell() -> None:
    cell = _mk_cell(exp=0.4, name="solo")
    assert pareto_frontier([cell]) == [cell]


def test_pareto_frontier_handles_empty() -> None:
    assert pareto_frontier([]) == []


# ---------------------------------------------------------------------------
# walk_forward_windows
# ---------------------------------------------------------------------------


def test_walk_forward_windows_default_step_is_test_bars() -> None:
    # 100 bars, train=60, test=20, step defaults to 20
    # windows: (0-60, 60-80), (20-80, 80-100)
    w = walk_forward_windows(n_bars=100, train_bars=60, test_bars=20)
    assert w == [(0, 60, 60, 80), (20, 80, 80, 100)]


def test_walk_forward_windows_custom_step_overlaps() -> None:
    # step=10 -> more overlap, more windows
    w = walk_forward_windows(n_bars=100, train_bars=60, test_bars=20, step=10)
    # test_starts: 60, 70, 80
    assert len(w) == 3
    assert [win[2] for win in w] == [60, 70, 80]


def test_walk_forward_windows_respects_train_end_equals_test_start() -> None:
    w = walk_forward_windows(n_bars=50, train_bars=30, test_bars=10)
    for train_start, train_end, test_start, test_end in w:
        assert train_end == test_start
        assert test_end == test_start + 10
        assert train_end - train_start == 30


def test_walk_forward_windows_raises_when_data_too_short() -> None:
    with pytest.raises(ValueError, match="too small"):
        walk_forward_windows(n_bars=10, train_bars=30, test_bars=10)


def test_walk_forward_windows_rejects_non_positive_sizes() -> None:
    with pytest.raises(ValueError):
        walk_forward_windows(n_bars=100, train_bars=0, test_bars=20)
    with pytest.raises(ValueError):
        walk_forward_windows(n_bars=100, train_bars=50, test_bars=0)
    with pytest.raises(ValueError):
        walk_forward_windows(n_bars=100, train_bars=50, test_bars=20, step=0)


# ---------------------------------------------------------------------------
# End-to-end: realistic Tier-B-shaped sweep
# ---------------------------------------------------------------------------


def test_end_to_end_sweep_picks_highest_expectancy_gate_passer() -> None:
    """Simulate a 3x3 grid where exactly 2 cells pass; the higher-expectancy
    one should be the winner."""
    grid = SweepGrid(
        params=[
            SweepParam(name="conf", values=[4.5, 5.5, 6.5]),
            SweepParam(name="risk", values=[0.005, 0.010, 0.015]),
        ],
    )

    def scorer(p: Mapping[str, Any]) -> CellScore:
        # Higher confluence = more selective = higher expectancy, fewer trades
        exp = (p["conf"] - 4.5) * 0.12  # 0.0 -> 0.24
        trades = int(200 / p["conf"])
        dd = 20.0 - p["conf"]  # higher conf -> lower dd
        return CellScore(
            expectancy_r=exp,
            max_dd_pct=dd,
            win_rate=0.5 + (p["conf"] - 4.5) * 0.02,
            n_trades=trades,
        )

    cells = run_sweep(grid, scorer, gate=Gate(min_expectancy_r=0.20, max_dd_pct=15.0))
    winner = pick_winner(cells)
    assert winner is not None
    assert winner.params["conf"] == 6.5  # highest expectancy passer
    assert winner.gate_pass is True
