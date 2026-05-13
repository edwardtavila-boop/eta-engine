"""Quantum-inspired QUBO solver (Wave-9, 2026-04-27).

QUBO = Quadratic Unconstrained Binary Optimization. Many financial
optimization problems map cleanly to QUBO form:

    minimize  x^T Q x
    where x in {0, 1}^n

Examples this module encodes:

  * portfolio_allocation_qubo:  Markowitz mean-variance with cardinality
                                constraint mapped to discrete weights
                                (e.g. each asset gets 0%, 25%, 50%, 75%, 100%)
  * sizing_basket_qubo:         choose K signals out of N to use this
                                hour, maximizing expected R while
                                penalizing correlated picks

Solver: simulated annealing with Metropolis acceptance. Pure stdlib.
This is the "quantum-inspired" leg -- it mimics the energy-minimization
picture that quantum annealers use, on classical hardware. For real
quantum (D-Wave, IBM QAOA), see ``cloud_adapter.py``.

Why this is real, not theatrics:

  * Actual papers (Mugel et al 2022 "Dynamic portfolio optimization
    with real datasets using quantum processors and quantum-inspired
    tensor networks") demonstrate hybrid wins on Markowitz problems
  * Simulated annealing alone reliably outperforms greedy heuristics
    on combinatorial sizing problems with > 8 assets
  * The QUBO formulation is the SAME whether you solve classically
    here or send it to a real quantum machine via cloud_adapter --
    so this module is also the staging ground for cloud calls
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ─── Core QUBO primitive ──────────────────────────────────────────


@dataclass
class QuboProblem:
    """A QUBO instance: symmetric Q matrix + variable labels.

    Q is stored as a dict-of-dicts (sparse-friendly): Q[i][j] is the
    coefficient of x_i * x_j. By convention diagonal entries are the
    linear contributions (since x_i^2 == x_i for binary x), off-
    diagonal entries are doubled-counted symmetric pairs.

    For dense problems pass ``q_matrix`` (a list of lists) at construct
    time; ``Q`` will be built lazily.
    """

    n_vars: int
    Q: dict[int, dict[int, float]] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)

    @classmethod
    def from_matrix(
        cls,
        q_matrix: list[list[float]],
        *,
        labels: list[str] | None = None,
    ) -> QuboProblem:
        n = len(q_matrix)
        out = cls(n_vars=n, labels=labels or [f"x{i}" for i in range(n)])
        for i in range(n):
            for j in range(n):
                v = q_matrix[i][j]
                if v == 0:
                    continue
                out.Q.setdefault(i, {})[j] = v
        return out

    def evaluate(self, x: list[int]) -> float:
        """Compute x^T Q x for the given binary vector."""
        e = 0.0
        for i, row in self.Q.items():
            xi = x[i]
            if xi == 0:
                continue
            for j, qij in row.items():
                e += qij * xi * x[j]
        return e


@dataclass
class SolverResult:
    x: list[int]
    energy: float
    n_iterations: int
    accepted_moves: int
    final_temperature: float
    labels: list[str] = field(default_factory=list)

    def selected_labels(self) -> list[str]:
        return [self.labels[i] for i in range(len(self.x)) if self.x[i] == 1]


def simulated_annealing_solve(
    problem: QuboProblem,
    *,
    n_iterations: int = 5_000,
    initial_temperature: float = 5.0,
    cooling_rate: float = 0.995,
    seed: int | None = None,
    starting_x: list[int] | None = None,
) -> SolverResult:
    """Solve a QUBO via simulated annealing.

    The annealing schedule is geometric: T_{k+1} = T_k * cooling_rate.
    At each iteration we flip a single random bit and accept with
    probability min(1, exp(-dE/T)). This is the classic Metropolis
    proposal that exact-quantum annealing approximates.

    Returns the best ``x`` seen across all iterations (not just the
    final state) so even cooling that gets stuck has a useful answer.
    """
    rng = random.Random(seed) if seed is not None else random.Random()
    n = problem.n_vars
    x = list(starting_x) if starting_x is not None else [rng.randint(0, 1) for _ in range(n)]
    energy = problem.evaluate(x)
    best_x = list(x)
    best_energy = energy
    T = initial_temperature  # noqa: N806 -- canonical SA notation
    accepted = 0

    for _ in range(n_iterations):
        # Single-bit flip proposal
        i = rng.randint(0, n - 1)
        x[i] = 1 - x[i]
        new_energy = problem.evaluate(x)
        delta = new_energy - energy

        # Metropolis acceptance
        if delta <= 0 or rng.random() < math.exp(-delta / max(T, 1e-9)):
            energy = new_energy
            accepted += 1
            if energy < best_energy:
                best_energy = energy
                best_x = list(x)
        else:
            # Reject: revert
            x[i] = 1 - x[i]

        T *= cooling_rate  # noqa: N806

    return SolverResult(
        x=best_x,
        energy=round(best_energy, 6),
        n_iterations=n_iterations,
        accepted_moves=accepted,
        final_temperature=round(T, 6),
        labels=problem.labels,
    )


# ─── Portfolio allocation encoder ─────────────────────────────────


def portfolio_allocation_qubo(
    *,
    expected_returns: list[float],
    covariance: list[list[float]],
    risk_aversion: float = 1.0,
    cardinality_min: int | None = None,
    cardinality_max: int | None = None,
    cardinality_penalty: float = 5.0,
    asset_labels: list[str] | None = None,
) -> QuboProblem:
    """Encode mean-variance portfolio selection as a QUBO.

    Each binary variable x_i = 1 means "include asset i in the
    selected basket". The objective is::

        minimize   risk_aversion * sum_ij Cov[i][j] * x_i * x_j
                 - sum_i  ExpRet[i] * x_i
                 + cardinality_penalty * (sum_i x_i - K)^2     [optional]

    where the cardinality penalty is added when min/max are supplied
    and K is the desired count. This is a STANDARD QUBO -- it is the
    same encoding D-Wave and IBM QAOA papers use for Markowitz.

    Returns a QuboProblem ready to feed to ``simulated_annealing_solve``
    or ``QuantumCloudAdapter.solve``.
    """
    n = len(expected_returns)
    if any(len(row) != n for row in covariance):
        raise ValueError("covariance must be n x n square")
    labels = asset_labels or [f"asset{i}" for i in range(n)]
    Q: dict[int, dict[int, float]] = {}  # noqa: N806 -- standard QUBO notation

    # Risk: + lambda * x^T Cov x
    for i in range(n):
        for j in range(n):
            v = risk_aversion * covariance[i][j]
            if v != 0:
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + v

    # Return: - mu^T x  -> place on the diagonal (since x_i^2 = x_i)
    for i in range(n):
        Q.setdefault(i, {})[i] = Q.get(i, {}).get(i, 0.0) - expected_returns[i]

    # Optional cardinality penalty: c * (sum_i x_i - K)^2
    if cardinality_min is not None or cardinality_max is not None:
        target_k = (
            (cardinality_min + cardinality_max) // 2
            if cardinality_min is not None and cardinality_max is not None
            else (cardinality_min if cardinality_min is not None else cardinality_max)
        )
        # (sum x - K)^2 = sum_ij x_i x_j - 2K sum_i x_i + K^2
        # The constant K^2 is dropped (doesn't affect argmin).
        for i in range(n):
            for j in range(n):
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + cardinality_penalty
            # -2K coefficient on each diagonal
            Q[i][i] = Q.get(i, {}).get(i, 0.0) - 2.0 * cardinality_penalty * target_k

    return QuboProblem(n_vars=n, Q=Q, labels=labels)


# ─── Discrete sizing encoder ──────────────────────────────────────


def sizing_basket_qubo(
    *,
    expected_r: list[float],
    pairwise_correlation: list[list[float]],
    correlation_penalty: float = 0.5,
    max_picks: int | None = None,
    pick_penalty: float = 2.0,
    signal_labels: list[str] | None = None,
) -> QuboProblem:
    """Choose which subset of N signals to fire this hour.

    Encodes:
      - reward (sum of expected R for picked signals)
      - penalty for picking highly-correlated signal pairs (so the
        portfolio doesn't double-count the same edge)
      - optional cardinality penalty if max_picks is supplied

    Standard sizing-day-job: "I have 12 candidate signals and only
    capital for 3-5 of them; which combo maximizes risk-adjusted
    return?" Classical greedy is myopic; this is global.
    """
    n = len(expected_r)
    if any(len(row) != n for row in pairwise_correlation):
        raise ValueError("pairwise_correlation must be n x n")
    labels = signal_labels or [f"sig{i}" for i in range(n)]
    Q: dict[int, dict[int, float]] = {}  # noqa: N806 -- standard QUBO notation

    # -E[r]_i on diagonal (we MAXIMIZE return -> negate)
    for i in range(n):
        Q.setdefault(i, {})[i] = -expected_r[i]

    # +penalty * |corr_ij| for off-diagonal pairs
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            cij = abs(pairwise_correlation[i][j])
            if cij > 0:
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + correlation_penalty * cij

    # Optional cardinality penalty: pick_penalty * (sum_x - max_picks)^2
    if max_picks is not None:
        for i in range(n):
            for j in range(n):
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + pick_penalty
            Q[i][i] = Q.get(i, {}).get(i, 0.0) - 2.0 * pick_penalty * max_picks

    return QuboProblem(n_vars=n, Q=Q, labels=labels)
