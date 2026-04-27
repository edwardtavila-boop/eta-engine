"""
EVOLUTIONARY TRADING ALGO  //  core.parameter_sweep
=======================================
Generic parameter-grid sweep engine with walk-forward stability scoring.

Used by:
  * tests/harness_open.py  -- open-testing scaffold that the driver leaves
    wired to this module
  * scripts/tier_b_param_sweep.py  -- bot-specific Tier-B sweep
  * scripts/master_tweaks.py  -- applies winning params back to bot configs

Design contract:
  1. **Pure python**. No external deps beyond stdlib + pydantic.
  2. **Deterministic**. Same inputs (grid + scorer) -> same winners.
  3. **Walk-forward aware**. Every cell can carry per-window scores so we
     can penalise unstable configs (high variance across splits).
  4. **Pareto-first**. A cell is only considered "winning" if it is not
     strictly dominated on (-expectancy, +dd, -stability).

This module is *not* a backtest. It just iterates a grid, calls a user-provided
scorer, and ranks the results. The scorer is the plug-in point -- a real
backtest, a synthetic generator, whatever the caller wires up.
"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Grid specification
# ---------------------------------------------------------------------------


class SweepParam(BaseModel):
    """A single parameter axis for the sweep grid."""

    name: str = Field(min_length=1)
    values: list[Any] = Field(min_length=1)

    def model_post_init(self, __context: object) -> None:
        if len(self.values) != len(set(map(repr, self.values))):
            raise ValueError(f"SweepParam '{self.name}' has duplicate values")


class SweepGrid(BaseModel):
    """A Cartesian-product grid of parameter axes."""

    params: list[SweepParam] = Field(min_length=1)

    def cardinality(self) -> int:
        n = 1
        for p in self.params:
            n *= len(p.values)
        return n

    def iter_combinations(self) -> Iterable[dict[str, Any]]:
        """Yield every {name: value} combo in the product order."""
        names = [p.name for p in self.params]
        axes = [p.values for p in self.params]
        for combo in itertools.product(*axes):
            yield dict(zip(names, combo, strict=True))


# ---------------------------------------------------------------------------
# Scoring contract
# ---------------------------------------------------------------------------


class CellScore(BaseModel):
    """The metrics a scorer returns for one param combination.

    Every field is in a consistent sign convention:
      * expectancy_r  > 0 is good
      * max_dd_pct    lower is better (stored as a positive number)
      * win_rate      [0, 1]
      * n_trades      sample-size proxy
      * walk_forward_scores  -- per-window expectancy_r for stability calc
    """

    expectancy_r: float
    max_dd_pct: float = Field(ge=0.0)
    win_rate: float = Field(ge=0.0, le=1.0)
    n_trades: int = Field(ge=0)
    total_return_pct: float = 0.0
    walk_forward_scores: list[float] = Field(default_factory=list)


class SweepCell(BaseModel):
    """One evaluated cell of the grid."""

    params: dict[str, Any]
    score: CellScore
    gate_pass: bool = False
    stability: float = Field(
        default=0.0,
        description=(
            "Lower is more stable. Standard deviation of walk_forward_scores, or 0.0 if fewer than 2 windows."
        ),
    )


class Gate(BaseModel):
    """Gate thresholds -- a cell passes iff all of these hold."""

    min_expectancy_r: float = 0.30
    max_dd_pct: float = 15.0
    min_trades: int = 30
    min_win_rate: float = 0.0

    def evaluate(self, s: CellScore) -> bool:
        return (
            s.expectancy_r >= self.min_expectancy_r
            and s.max_dd_pct <= self.max_dd_pct
            and s.n_trades >= self.min_trades
            and s.win_rate >= self.min_win_rate
        )


# ---------------------------------------------------------------------------
# Sweep engine
# ---------------------------------------------------------------------------

Scorer = Callable[[Mapping[str, Any]], CellScore]


def run_sweep(
    grid: SweepGrid,
    scorer: Scorer,
    gate: Gate | None = None,
) -> list[SweepCell]:
    """Evaluate every combination in ``grid`` via ``scorer``.

    Returns the raw list of cells in grid-iteration order. Use
    ``rank_cells`` / ``pick_winner`` / ``pareto_frontier`` on the result.

    ``scorer`` must be a pure function of the params dict -- no shared
    mutable state -- so that the sweep is trivially parallelisable later.
    """
    g = gate or Gate()
    cells: list[SweepCell] = []
    for combo in grid.iter_combinations():
        s = scorer(combo)
        stab = statistics.pstdev(s.walk_forward_scores) if len(s.walk_forward_scores) >= 2 else 0.0
        cells.append(
            SweepCell(
                params=dict(combo),
                score=s,
                gate_pass=g.evaluate(s),
                stability=round(stab, 6),
            ),
        )
    return cells


# ---------------------------------------------------------------------------
# Ranking + winner selection
# ---------------------------------------------------------------------------


def rank_cells(cells: list[SweepCell]) -> list[SweepCell]:
    """Return cells sorted from best to worst.

    Ordering (stable, deterministic):
      1. gate_pass descending (pass first)
      2. expectancy_r descending
      3. max_dd_pct ascending
      4. stability ascending (more stable first)
      5. n_trades descending (more samples first)
    """
    return sorted(
        cells,
        key=lambda c: (
            not c.gate_pass,
            -c.score.expectancy_r,
            c.score.max_dd_pct,
            c.stability,
            -c.score.n_trades,
        ),
    )


def pick_winner(cells: list[SweepCell]) -> SweepCell | None:
    """Pick the best cell.

    If any cell passes the gate, return the highest-ranked passer. Otherwise
    fall back to the closest-to-passing (highest expectancy_r, lowest dd).
    Returns None only when ``cells`` is empty.
    """
    if not cells:
        return None
    ranked = rank_cells(cells)
    return ranked[0]


def pareto_frontier(cells: list[SweepCell]) -> list[SweepCell]:
    """Return the Pareto-optimal subset on (expectancy_r, -max_dd_pct, -stability).

    A cell is on the frontier iff no other cell dominates it on every
    objective. Ties do not dominate.
    """
    frontier: list[SweepCell] = []
    for a in cells:
        dominated = False
        for b in cells:
            if a is b:
                continue
            if _dominates(b, a):
                dominated = True
                break
        if not dominated:
            frontier.append(a)
    return frontier


def _dominates(a: SweepCell, b: SweepCell) -> bool:
    """True if ``a`` strictly dominates ``b`` on (expectancy up, dd down, stab down)."""
    ge_exp = a.score.expectancy_r >= b.score.expectancy_r
    le_dd = a.score.max_dd_pct <= b.score.max_dd_pct
    le_stab = a.stability <= b.stability
    strict = (
        a.score.expectancy_r > b.score.expectancy_r
        or a.score.max_dd_pct < b.score.max_dd_pct
        or a.stability < b.stability
    )
    return ge_exp and le_dd and le_stab and strict


# ---------------------------------------------------------------------------
# Walk-forward helper
# ---------------------------------------------------------------------------


def walk_forward_windows(
    n_bars: int,
    train_bars: int,
    test_bars: int,
    step: int | None = None,
) -> list[tuple[int, int, int, int]]:
    """Generate (train_start, train_end, test_start, test_end) indices.

    Non-overlapping test windows slide forward by ``step`` (default = test_bars).
    Raises ValueError if ``n_bars`` can't fit at least one window.
    """
    if train_bars <= 0 or test_bars <= 0:
        raise ValueError("train_bars and test_bars must be positive")
    s = step if step is not None else test_bars
    if s <= 0:
        raise ValueError("step must be positive")
    if n_bars < train_bars + test_bars:
        raise ValueError(
            f"n_bars={n_bars} too small for train={train_bars} + test={test_bars}",
        )
    out: list[tuple[int, int, int, int]] = []
    test_start = train_bars
    while test_start + test_bars <= n_bars:
        train_start = test_start - train_bars
        train_end = test_start
        test_end = test_start + test_bars
        out.append((train_start, train_end, test_start, test_end))
        test_start += s
    return out
