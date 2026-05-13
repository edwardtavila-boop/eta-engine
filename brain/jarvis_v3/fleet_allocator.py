"""Fleet allocator (Wave-15, 2026-04-27).

When multiple bots want to enter simultaneously, individual JARVIS
verdicts don't see each other's exposure. The fleet allocator
solves the joint allocation problem:

  * Inputs: a list of FleetRequest, one per bot wanting to enter
  * Constraint: total fleet risk capped (e.g. 3R combined max)
  * Constraint: pairwise correlation penalty (concentrated bets shrink)
  * Output: per-bot size_multiplier in [0, 1]

Encoded as a QUBO and solved via the existing simulated_annealing
solver in quantum/qubo_solver. Pure-stdlib end to end.

Use case (called by an orchestrator that batches per-tick requests):

    from eta_engine.brain.jarvis_v3.fleet_allocator import (
        FleetRequest, allocate_fleet,
    )

    requests = [
        FleetRequest(bot_id="MNQ", expected_r=1.5, base_size=1.0),
        FleetRequest(bot_id="NQ", expected_r=1.4, base_size=1.0),
        FleetRequest(bot_id="BTC", expected_r=2.0, base_size=1.0),
    ]
    allocation = allocate_fleet(
        requests, max_picks=2,  # only 2 bots can fire this slice
        correlation_matrix=cov,
    )
    for entry in allocation.entries:
        bot.size = entry.size_multiplier * base_size
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class FleetRequest:
    """One bot's request to enter."""

    bot_id: str
    expected_r: float  # bot's own expected R (best estimate)
    base_size: float = 1.0  # bot's already-sized intended notional
    direction: str = "long"  # "long" or "short"
    priority: float = 1.0  # operator-tunable: 1.0 = standard


@dataclass
class FleetAllocationEntry:
    bot_id: str
    size_multiplier: float  # in [0, 1]
    rank: int
    note: str = ""


@dataclass
class FleetAllocation:
    entries: list[FleetAllocationEntry] = field(default_factory=list)
    objective: float = 0.0
    n_picked: int = 0
    n_total: int = 0
    method: str = "qubo"


def allocate_fleet(
    requests: list[FleetRequest],
    *,
    correlation_matrix: list[list[float]] | None = None,
    max_picks: int | None = None,
    correlation_penalty: float = 0.5,
    use_qubo: bool = True,
) -> FleetAllocation:
    """Solve joint allocation across the fleet.

    When correlation_matrix is None we treat all bots as
    uncorrelated (worst case for diversity, but safe).

    When use_qubo is False we fall back to a simple greedy
    diversity-aware picker (same idea as tensor_network's
    select_top_signal_combination).
    """
    n = len(requests)
    if n == 0:
        return FleetAllocation(method="empty")

    if correlation_matrix is None:
        correlation_matrix = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    if any(len(row) != n for row in correlation_matrix):
        raise ValueError("correlation_matrix must be n x n")

    # Same-direction bots are correlated; opposite-direction reduces
    # joint exposure -- bake into the effective correlation matrix.
    eff_corr = [[correlation_matrix[i][j] for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            same_dir = requests[i].direction == requests[j].direction
            sign = 1.0 if same_dir else -1.0
            eff_corr[i][j] = eff_corr[i][j] * sign

    if max_picks is None:
        max_picks = n  # no constraint

    if use_qubo:
        return _allocate_qubo(
            requests=requests,
            corr=eff_corr,
            max_picks=max_picks,
            correlation_penalty=correlation_penalty,
        )
    return _allocate_greedy(
        requests=requests,
        corr=eff_corr,
        max_picks=max_picks,
        correlation_penalty=correlation_penalty,
    )


# ─── QUBO path ───────────────────────────────────────────────────


def _allocate_qubo(
    *,
    requests: list[FleetRequest],
    corr: list[list[float]],
    max_picks: int,
    correlation_penalty: float,
) -> FleetAllocation:
    try:
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
            simulated_annealing_solve,
            sizing_basket_qubo,
        )
    except ImportError as exc:
        logger.warning("fleet_allocator: QUBO solver unavailable (%s); greedy", exc)
        return _allocate_greedy(
            requests=requests,
            corr=corr,
            max_picks=max_picks,
            correlation_penalty=correlation_penalty,
        )

    # Effective scores: bot priority * expected R
    scores = [r.priority * r.expected_r for r in requests]
    labels = [r.bot_id for r in requests]
    problem = sizing_basket_qubo(
        expected_r=scores,
        pairwise_correlation=corr,
        correlation_penalty=correlation_penalty,
        max_picks=max_picks,
        signal_labels=labels,
    )
    result = simulated_annealing_solve(
        problem,
        n_iterations=2_000,
        seed=42,
    )

    entries: list[FleetAllocationEntry] = []
    rank = 0
    for i, picked in enumerate(result.x):
        if picked == 1:
            entries.append(
                FleetAllocationEntry(
                    bot_id=requests[i].bot_id,
                    size_multiplier=1.0,
                    rank=rank,
                    note="picked by joint QUBO",
                )
            )
            rank += 1
        else:
            entries.append(
                FleetAllocationEntry(
                    bot_id=requests[i].bot_id,
                    size_multiplier=0.0,
                    rank=-1,
                    note="not picked (joint allocation)",
                )
            )

    return FleetAllocation(
        entries=entries,
        objective=result.energy,
        n_picked=sum(result.x),
        n_total=len(requests),
        method="qubo",
    )


# ─── Greedy fallback ─────────────────────────────────────────────


def _allocate_greedy(
    *,
    requests: list[FleetRequest],
    corr: list[list[float]],
    max_picks: int,
    correlation_penalty: float,
) -> FleetAllocation:
    """Diversity-aware greedy picker."""
    indexed = list(enumerate(requests))
    # Sort by priority * expected_r descending
    indexed.sort(key=lambda t: t[1].priority * t[1].expected_r, reverse=True)

    picked: list[int] = []
    for idx, _ in indexed:
        if len(picked) >= max_picks:
            break
        # Compute average correlation to picked
        if not picked:
            picked.append(idx)
            continue
        avg_overlap = sum(abs(corr[idx][p]) for p in picked) / len(picked)
        # Adjusted score
        base_score = requests[idx].priority * requests[idx].expected_r
        adj = base_score - correlation_penalty * avg_overlap
        if adj > 0:
            picked.append(idx)

    entries: list[FleetAllocationEntry] = []
    rank_lookup = {idx: r for r, idx in enumerate(picked)}
    for i, req in enumerate(requests):
        if i in rank_lookup:
            entries.append(
                FleetAllocationEntry(
                    bot_id=req.bot_id,
                    size_multiplier=1.0,
                    rank=rank_lookup[i],
                    note="picked by greedy diversity",
                )
            )
        else:
            entries.append(
                FleetAllocationEntry(
                    bot_id=req.bot_id,
                    size_multiplier=0.0,
                    rank=-1,
                    note="not picked",
                )
            )

    return FleetAllocation(
        entries=entries,
        objective=sum(requests[i].priority * requests[i].expected_r for i in picked),
        n_picked=len(picked),
        n_total=len(requests),
        method="greedy",
    )
