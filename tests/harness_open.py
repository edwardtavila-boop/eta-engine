# === OPEN FOR FINAL MASTER TWEAKS ===
"""
EVOLUTIONARY TRADING ALGO  //  tests.harness_open
=====================================
THE OPEN TESTING HARNESS.
Parameter sweeps, A/B comparators, regime-sliced evaluation.

2026-04-17: wired to core.parameter_sweep / core.master_tweaks so the
harness is no longer a stub -- the three entry points run real logic and
can be plugged into any bot by supplying a ``scorer`` callable.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping
from typing import Any

from eta_engine.brain.regime import RegimeType
from eta_engine.core.parameter_sweep import (
    CellScore,
    Gate,
    SweepGrid,
    SweepParam,
    pareto_frontier,
    pick_winner,
    rank_cells,
    run_sweep,
)

Scorer = Callable[[Mapping[str, Any]], CellScore]

# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------


def run_parameter_sweep(
    param_ranges: dict[str, list[Any]],
    scorer: Scorer,
    gate: Gate | None = None,
) -> list[dict[str, Any]]:
    """Sweep all combinations of parameters, collect results.

    Args:
        param_ranges: {"risk_pct": [0.005, 0.01, 0.02], "atr_mult": [1.5, 2.0, 3.0]}
        scorer:       pure function (params_dict) -> CellScore. This is the
                      plug-in for the real backtest -- wire it up per bot.
        gate:         optional Gate with custom thresholds.

    Returns:
        List of dicts ordered best-first (rank_cells):
          [{params, expectancy_r, max_dd_pct, win_rate, n_trades,
            stability, gate_pass}, ...]
    """
    if not param_ranges:
        return []
    grid = SweepGrid(
        params=[SweepParam(name=k, values=list(v)) for k, v in param_ranges.items()],
    )
    cells = run_sweep(grid, scorer, gate=gate)
    ranked = rank_cells(cells)
    return [
        {
            "params": dict(c.params),
            "expectancy_r": c.score.expectancy_r,
            "max_dd_pct": c.score.max_dd_pct,
            "win_rate": c.score.win_rate,
            "n_trades": c.score.n_trades,
            "stability": c.stability,
            "gate_pass": c.gate_pass,
        }
        for c in ranked
    ]


def pareto_frontier_for(
    param_ranges: dict[str, list[Any]],
    scorer: Scorer,
) -> list[dict[str, Any]]:
    """Return only the Pareto-optimal cells from a sweep.

    Useful when you don't want to commit to a single winner but want the
    efficient frontier across (expectancy, dd, stability).
    """
    if not param_ranges:
        return []
    grid = SweepGrid(
        params=[SweepParam(name=k, values=list(v)) for k, v in param_ranges.items()],
    )
    cells = run_sweep(grid, scorer)
    return [
        {
            "params": dict(c.params),
            "expectancy_r": c.score.expectancy_r,
            "max_dd_pct": c.score.max_dd_pct,
            "stability": c.stability,
            "gate_pass": c.gate_pass,
        }
        for c in pareto_frontier(cells)
    ]


# ---------------------------------------------------------------------------
# Forward test A/B comparator
# ---------------------------------------------------------------------------


def run_forward_test_comparator(
    scorer_a: Scorer,
    scorer_b: Scorer,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Compare two bot configs / strategies on the same param vector.

    Each ``scorer_*`` is a bot-backed scorer that already holds its own data
    context. The comparator just runs them both against the shared param
    set and reports the spread.

    Returns:
        {
            "bot_a": {"expectancy_r", "max_dd_pct", "win_rate"},
            "bot_b": {...},
            "winner": "bot_a" | "bot_b" | "tie",
            "edge_pct": float,        # |a_exp - b_exp| / max(|a|, |b|, 0.001) * 100
            "edge_r": float,          # a_exp - b_exp (signed)
        }
    """
    a = scorer_a(params)
    b = scorer_b(params)
    edge_r = a.expectancy_r - b.expectancy_r
    if a.expectancy_r > b.expectancy_r:
        winner = "bot_a"
    elif b.expectancy_r > a.expectancy_r:
        winner = "bot_b"
    else:
        winner = "tie"
    denom = max(abs(a.expectancy_r), abs(b.expectancy_r), 0.001)
    edge_pct = abs(edge_r) / denom * 100.0
    return {
        "bot_a": {
            "expectancy_r": a.expectancy_r,
            "max_dd_pct": a.max_dd_pct,
            "win_rate": a.win_rate,
        },
        "bot_b": {
            "expectancy_r": b.expectancy_r,
            "max_dd_pct": b.max_dd_pct,
            "win_rate": b.win_rate,
        },
        "winner": winner,
        "edge_pct": round(edge_pct, 2),
        "edge_r": round(edge_r, 4),
    }


# ---------------------------------------------------------------------------
# Regime-sliced evaluator
# ---------------------------------------------------------------------------


def run_regime_slice_evaluator(
    scorer: Scorer,
    data: list[dict[str, float]],
    regimes: list[RegimeType],
    params: dict[str, Any] | None = None,
) -> dict[str, dict[str, float]]:
    """Evaluate a bot sliced by regime.

    For each regime present in ``regimes`` we:
      1. Filter bars where that regime was active.
      2. Call ``scorer`` once per regime with {**params, "regime": regime_name,
         "bars": filtered_bars} so scorers can adapt.

    Returns:
        {
            "TRENDING": {"expectancy_r": 1.2, "max_dd_pct": 3.4,
                         "win_rate": 0.58, "num_bars": 200},
            "RANGING":  {...},
            ...
        }
    """
    if len(data) != len(regimes):
        raise ValueError(
            f"data ({len(data)}) and regimes ({len(regimes)}) must match",
        )
    params = dict(params or {})
    regime_bars: dict[str, list[dict[str, float]]] = {}
    for bar, regime in zip(data, regimes, strict=True):
        key = regime.value if isinstance(regime, RegimeType) else str(regime)
        regime_bars.setdefault(key, []).append(bar)

    results: dict[str, dict[str, float]] = {}
    for regime_name, bars in regime_bars.items():
        regime_params = {**params, "regime": regime_name, "bars": bars}
        s = scorer(regime_params)
        results[regime_name] = {
            "expectancy_r": s.expectancy_r,
            "max_dd_pct": s.max_dd_pct,
            "win_rate": s.win_rate,
            "num_bars": float(len(bars)),
        }
    return results


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------


def summarize_sweep(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Produce a one-shot summary of a sweep run for dashboards / tests."""
    if not results:
        return {
            "n_cells": 0,
            "n_pass": 0,
            "best_expectancy_r": 0.0,
            "median_expectancy_r": 0.0,
            "winner": None,
        }
    passers = [r for r in results if r["gate_pass"]]
    exp_values = [r["expectancy_r"] for r in results]
    return {
        "n_cells": len(results),
        "n_pass": len(passers),
        "best_expectancy_r": max(exp_values),
        "median_expectancy_r": statistics.median(exp_values),
        "winner": results[0],  # rank_cells put the winner first
    }


def pick_winner_from_results(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the winning cell from a ``run_parameter_sweep`` result list.

    results[0] is already the ranked winner, but we make the intent explicit
    for callers.
    """
    return results[0] if results else None


# Re-export for harness consumers.
__all__ = [
    "CellScore",
    "Gate",
    "pareto_frontier_for",
    "pick_winner",
    "pick_winner_from_results",
    "run_forward_test_comparator",
    "run_parameter_sweep",
    "run_regime_slice_evaluator",
    "summarize_sweep",
]
