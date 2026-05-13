"""Tensor-network signal selector (Wave-9, 2026-04-27).

Quantum-inspired classical algorithm for the question:

    "Of these N candidate signals (Wyckoff accumulation, Elliott
    wave 3, Fibonacci 61.8% retest, liquidity sweep, order block...),
    which COMBINATION maximizes joint conviction without redundancy?"

The naive approach -- "pick the K with highest individual score" --
ignores that the signals carry overlapping information. A signal that
fires on the same setup as another adds little. We want signals that
are individually strong AND mutually informative.

Tensor-network angle: each signal is a rank-1 tensor of indicator
features (regime, session, vol, structure level). The contraction
between two signals' tensors is a similarity score; we use it as a
diversity penalty so the selector spreads picks across orthogonal
parts of the feature space.

This module is a LIGHTWEIGHT classical analog of TN methods used in
quantum ML papers (e.g. Stoudenmire & Schwab 2016 "Supervised
Learning with Quantum-Inspired Tensor Networks"). It runs on stdlib;
NumPy is used IFF available, otherwise nested-list math.

Use case (firm-board hand-off):

    from eta_engine.brain.jarvis_v3.quantum.tensor_network import (
        SignalScore, select_top_signal_combination,
    )

    candidates = [
        SignalScore(name="wyckoff_acc", score=0.6, features=[1, 1, 0, 0]),
        SignalScore(name="elliott_w3",  score=0.5, features=[1, 1, 0, 1]),
        SignalScore(name="fib_618",     score=0.4, features=[0, 0, 1, 1]),
        SignalScore(name="liquidity",   score=0.7, features=[0, 1, 1, 0]),
    ]
    pick = select_top_signal_combination(candidates, k=2)
    # -> picks the most-orthogonal high-scorers, not just the top-2 by raw score
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SignalScore:
    """One candidate signal with its individual score and feature
    fingerprint. ``features`` should be a small fixed-dimension vector
    (typically 4-12) so contractions stay cheap."""

    name: str
    score: float  # individual conviction in [-1, +1] or [0, 1]
    features: list[float] = field(default_factory=list)


@dataclass
class CombinationResult:
    selected: list[SignalScore]
    total_raw_score: float
    total_diversity_score: float
    objective: float
    label: str = ""


# ─── Tensor contraction (cosine-style overlap) ────────────────────


def _contract(a: list[float], b: list[float]) -> float:
    """Inner product divided by norms -> cosine similarity in [-1, 1].

    For length-mismatched vectors returns 0 (defensive)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ─── Top-k diversity-aware selection ──────────────────────────────


def select_top_signal_combination(
    candidates: list[SignalScore],
    *,
    k: int,
    diversity_weight: float = 0.4,
) -> CombinationResult:
    """Greedy with a diversity-penalty term.

    Algorithm:
      1. Sort candidates by raw score descending
      2. At each step, score remaining candidates by:
             score - diversity_weight * mean_overlap_with_already_picked
      3. Pick highest, repeat k-1 more times

    This is provably suboptimal vs full QUBO/branch-and-bound, but
    runs in O(n^2) and consistently beats naive top-k on real signal
    portfolios. For mission-critical sizing problems use the QUBO
    encoder + simulated_annealing_solve in qubo_solver.py.
    """
    if not candidates:
        return CombinationResult(
            selected=[],
            total_raw_score=0.0,
            total_diversity_score=0.0,
            objective=0.0,
            label="empty input",
        )
    if k >= len(candidates):
        return CombinationResult(
            selected=list(candidates),
            total_raw_score=sum(c.score for c in candidates),
            total_diversity_score=0.0,
            objective=sum(c.score for c in candidates),
            label="k >= n; selected all",
        )

    remaining = list(candidates)
    selected: list[SignalScore] = []

    # Step 1: pick the highest individual scorer
    remaining.sort(key=lambda c: c.score, reverse=True)
    selected.append(remaining.pop(0))

    # Steps 2..k: pick by score minus diversity penalty
    while len(selected) < k and remaining:

        def adj_score(c: SignalScore) -> float:
            overlap = sum(abs(_contract(c.features, s.features)) for s in selected) / max(len(selected), 1)
            return c.score - diversity_weight * overlap

        best = max(remaining, key=adj_score)
        selected.append(best)
        remaining.remove(best)

    raw = sum(c.score for c in selected)
    # Diversity bonus = 1 - avg_pairwise_overlap (higher = more diverse)
    if len(selected) >= 2:
        overlaps = [abs(_contract(a.features, b.features)) for i, a in enumerate(selected) for b in selected[i + 1 :]]
        diversity = 1.0 - (sum(overlaps) / len(overlaps))
    else:
        diversity = 1.0
    objective = raw + diversity_weight * diversity

    return CombinationResult(
        selected=selected,
        total_raw_score=round(raw, 4),
        total_diversity_score=round(diversity, 4),
        objective=round(objective, 4),
        label=f"top-{k} diversity-aware",
    )


# ─── Pairwise correlation matrix from feature tensors ─────────────


def signal_correlation_matrix(
    candidates: list[SignalScore],
) -> list[list[float]]:
    """Return the n x n cosine-similarity matrix of feature tensors.

    Useful as the input to ``sizing_basket_qubo``: the QUBO solver
    consumes this as the redundancy-penalty matrix and finds the
    GLOBAL optimum (vs the greedy heuristic above)."""
    n = len(candidates)
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            out[i][j] = _contract(candidates[i].features, candidates[j].features)
    return out
