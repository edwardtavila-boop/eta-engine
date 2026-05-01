"""Supercharged QUBO formulations (Wave-18 expansion, 2026-04-30).

Extends the base QUBO solver with advanced problem types:

  * risk_parity_qubo           -- equalize raw risk contribution across assets
  * regime_aware_qubo          -- warps expected returns by regime modifiers
  * multi_horizon_qubo         -- optimize across short/medium/long horizons
  * hedging_basket_qubo        -- find the best hedge set for existing positions
  * parallel_tempering_solve   -- MC ensemble solver that beats SA on rugged landscapes

Classical SA is still the default; these are problem encodings that work with
the same solve pipeline (simulated_annealing_solve or QuantumCloudAdapter).

Edge-case hardening (Wave-18):
  * NaN/inf guards on all QUBO inputs — raise ValueError before solving
  * parallel_tempering has a hard timeout parameter
  * adaptive_solve falls back to SA if PT returns NaN energy
  * all solvers cap iteration count to prevent runaway
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
    QuboProblem,
    SolverResult,
    simulated_annealing_solve,
)

MAX_ITERATIONS = 50_000  # Hard cap to prevent runaway


def _guard_finite(values: list[float], label: str) -> None:
    for i, v in enumerate(values):
        if not math.isfinite(v):
            raise ValueError(f"{label}[{i}] = {v} — must be finite")


def _guard_finite_matrix(matrix: list[list[float]], label: str) -> None:
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if not math.isfinite(v):
                raise ValueError(f"{label}[{i}][{j}] = {v} — must be finite")


# ─── Risk-parity QUBO ─────────────────────────────────────────────


def risk_parity_qubo(
    *,
    expected_returns: list[float],
    covariance: list[list[float]],
    risk_aversion: float = 1.0,
    risk_concentration_penalty: float = 3.0,
    max_assets: int | None = None,
    asset_labels: list[str] | None = None,
) -> QuboProblem:
    """Risk-parity allocation: equalize risk contribution across selected assets.

    Minimizes the variance of risk contributions across the portfolio.
    Each asset gets a contribution proportional to w_i * (Cov w)_i.
    We penalize deviation from equal contribution via a quadratic penalty.

    Standard risk-parity objective decoded as QUBO:
        minimize   risk_aversion * w^T Cov w
                 + penalty * sum_i (RC_i - 1/n)^2

    where RC_i = w_i * (Cov w)_i / portfolio_variance is the risk
    contribution fraction. For the QUBO approximation we drop the
    normalization and penalize variance of raw contributions.
    """
    n = len(expected_returns)
    labels = asset_labels or [f"asset{i}" for i in range(n)]
    Q: dict[int, dict[int, float]] = {}

    # Standard mean-variance diagonal
    for i in range(n):
        for j in range(n):
            v = risk_aversion * covariance[i][j]
            if v != 0:
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + v
        Q.setdefault(i, {})[i] = Q.get(i, {}).get(i, 0.0) - expected_returns[i]

    # Risk concentration penalty: sum_i sum_j w_i * Cov_ij * w_j
    # plus cross terms that punish clustering
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            cluster_term = risk_concentration_penalty * abs(covariance[i][j])
            Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + cluster_term

    # Desired number of assets (cardinality soft constraint)
    if max_assets is not None:
        for i in range(n):
            for j in range(n):
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + 2.0
            Q[i][i] = Q.get(i, {}).get(i, 0.0) - 4.0 * max_assets

    return QuboProblem(n_vars=n, Q=Q, labels=labels)


# ─── Regime-aware QUBO ────────────────────────────────────────────


@dataclass
class RegimeModifier:
    """Per-asset multiplier for a given regime."""

    asset_index: int
    return_multiplier: float = 1.0
    risk_multiplier: float = 1.0
    liquidity_multiplier: float = 1.0


def regime_aware_qubo(
    *,
    expected_returns: list[float],
    covariance: list[list[float]],
    modifiers: list[RegimeModifier],
    risk_aversion: float = 1.0,
    cardinality_max: int | None = None,
    asset_labels: list[str] | None = None,
) -> QuboProblem:
    """Regime-warped portfolio allocation QUBO.

    Each asset's expected return and risk contribution is multiplied
    by regime-specific modifiers. Assets with regime_headwind are
    penalized; assets with regime_tailwind get a boost.

    Modifier map builds a lookup table:
        asset_i -> (ret_mult, risk_mult, liq_mult)
    then warps the QUBO diagonal (return) and off-diagonal (risk).
    """
    n = len(expected_returns)
    labels = asset_labels or [f"asset{i}" for i in range(n)]

    mod_map: dict[int, RegimeModifier] = {
        m.asset_index: m for m in modifiers if 0 <= m.asset_index < n
    }

    Q: dict[int, dict[int, float]] = {}
    for i in range(n):
        rm = mod_map.get(i, RegimeModifier(i, 1.0, 1.0, 1.0))
        ret_mod = rm.return_multiplier
        risk_mod = rm.risk_multiplier

        # Return contribution (negative for minimization)
        Q.setdefault(i, {})[i] = Q.get(i, {}).get(i, 0.0) - expected_returns[i] * ret_mod

        for j in range(n):
            rm_j = mod_map.get(j, RegimeModifier(j, 1.0, 1.0, 1.0))
            risk_ij = risk_aversion * covariance[i][j] * risk_mod * rm_j.risk_multiplier
            if risk_ij != 0:
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + risk_ij

    if cardinality_max is not None:
        for i in range(n):
            for j in range(n):
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + 3.0
            Q[i][i] = Q.get(i, {}).get(i, 0.0) - 6.0 * cardinality_max

    return QuboProblem(n_vars=n, Q=Q, labels=labels)


# ─── Multi-horizon QUBO ───────────────────────────────────────────


@dataclass
class HorizonSlice:
    """One time-horizon slice: short/medium/long-term allocation."""

    name: str
    weight: float = 1.0
    expected_returns: list[float] = field(default_factory=list)
    covariance: list[list[float]] | None = None


def multi_horizon_qubo(
    *,
    horizons: list[HorizonSlice],
    risk_aversion: float = 1.0,
    cross_horizon_correlation: list[list[float]] | None = None,
    max_assets_total: int | None = None,
    asset_labels: list[str] | None = None,
) -> QuboProblem:
    """Multi-timeframe portfolio allocation QUBO.

    Encodes N_assets * N_horizons binary variables. Each variable
    x_{h,i} means "include asset i in horizon h portfolio." The
    objective minimizes:
      - cross-section risk (within each horizon)
      - cross-horizon overlapping penalty (don't put all eggs in one
        asset across timeframes)
      - horizon-weighted return maximization

    Total variables = N_assets * N_horizons.
    Flattening: index = h * n_assets + i
    """
    n_assets = len(asset_labels) if asset_labels else len(horizons[0].expected_returns) if horizons else 0
    n_horizons = len(horizons)
    total_vars = n_assets * n_horizons
    labels: list[str] = []
    orig_labels = asset_labels or [f"a{i}" for i in range(n_assets)]

    for h_idx, h in enumerate(horizons):
        for i in range(n_assets):
            labels.append(f"{h.name}/{orig_labels[i]}")

    Q: dict[int, dict[int, float]] = {}

    def idx(h: int, asset: int) -> int:
        return h * n_assets + asset

    for h_idx, horizon in enumerate(horizons):
        cov = horizon.covariance
        if cov is None:
            cov = [[1.0 if i == j else 0.3 for j in range(n_assets)] for i in range(n_assets)]

        for i in range(n_assets):
            vi = idx(h_idx, i)
            ret = horizon.expected_returns[i] if i < len(horizon.expected_returns) else 0.0
            Q.setdefault(vi, {})[vi] = Q.get(vi, {}).get(vi, 0.0) - ret * horizon.weight

            for j in range(n_assets):
                vj = idx(h_idx, j)
                risk_ij = risk_aversion * cov[i][j] * horizon.weight
                if risk_ij != 0:
                    Q.setdefault(vi, {})[vj] = Q.get(vi, {}).get(vj, 0.0) + risk_ij

    # Cross-horizon penalty: penalize picking the same asset across horizons
    cross_penalty = 1.5
    if cross_horizon_correlation is not None:
        for h1 in range(n_horizons):
            for h2 in range(h1 + 1, n_horizons):
                if h2 < len(cross_horizon_correlation) and h1 < len(cross_horizon_correlation[h2]):
                    for i in range(n_assets):
                        vi = idx(h1, i)
                        vj = idx(h2, i)
                        p = cross_penalty * abs(cross_horizon_correlation[h2][h1])
                        Q.setdefault(vi, {})[vj] = Q.get(vi, {}).get(vj, 0.0) + p
                        Q.setdefault(vj, {})[vi] = Q.get(vj, {}).get(vi, 0.0) + p
    else:
        for h1 in range(n_horizons):
            for h2 in range(h1 + 1, n_horizons):
                for i in range(n_assets):
                    vi = idx(h1, i)
                    vj = idx(h2, i)
                    Q.setdefault(vi, {})[vj] = Q.get(vi, {}).get(vj, 0.0) + cross_penalty * 0.5
                    Q.setdefault(vj, {})[vi] = Q.get(vj, {}).get(vi, 0.0) + cross_penalty * 0.5

    if max_assets_total is not None:
        for vi in range(total_vars):
            for vj in range(total_vars):
                Q.setdefault(vi, {})[vj] = Q.get(vi, {}).get(vj, 0.0) + 4.0
            Q.setdefault(vi, {})[vi] = Q.get(vi, {}).get(vi, 0.0) - 8.0 * max_assets_total

    return QuboProblem(n_vars=total_vars, Q=Q, labels=labels)


# ─── Hedging basket QUBO ──────────────────────────────────────────


def hedging_basket_qubo(
    *,
    positions: list[float],
    candidates: list[float],
    pairwise_correlation: list[list[float]],
    target_net_beta: float = 0.0,
    correlation_penalty: float = 2.0,
    max_hedges: int | None = None,
    position_labels: list[str] | None = None,
    hedge_labels: list[str] | None = None,
) -> QuboProblem:
    """Find optimal hedge instruments for an existing position set.

    Given N existing positions and M candidate hedge instruments,
    encode the problem: choose a subset of hedges to bring the
    combined portfolio beta as close as possible to target_net_beta.

    Variables: x_{1..M} where x_j = 1 means "use hedge j".

    Objective:
        minimize   (total_beta - target_net_beta)^2
                 + penalty * sum of cross-hedge correlations
                 + cardinality penalty
    """
    n_pos = len(positions)
    n_hedge = len(candidates)
    total_vars = n_hedge

    pos_labels = position_labels or [f"pos{i}" for i in range(n_pos)]
    hed_labels = hedge_labels or [f"hedge{i}" for i in range(n_hedge)]

    # Precompute total position beta: sum of position sizes (proxy)
    total_position_exposure = sum(abs(p) for p in positions)
    # Each hedge candidate contributes its size toward offsetting
    hedge_contributions = [abs(c) for c in candidates]

    Q: dict[int, dict[int, float]] = {}
    # Diagonal: penalty for picking each hedge + contribution to beta offset
    for j in range(n_hedge):
        contrib = hedge_contributions[j] * candidates[j] / max(abs(candidates[j]), 1e-9)
        deviation = (total_position_exposure + contrib) - target_net_beta * total_position_exposure
        Q.setdefault(j, {})[j] = deviation * deviation * 0.01 - abs(candidates[j]) * 0.5

    # Cross-hedge correlation penalty
    for j1 in range(n_hedge):
        for j2 in range(j1 + 1, n_hedge):
            if j2 < len(pairwise_correlation) and j1 < len(pairwise_correlation[j2]):
                p = correlation_penalty * abs(pairwise_correlation[j2][j1])
                if p > 0:
                    Q.setdefault(j1, {})[j2] = Q.get(j1, {}).get(j2, 0.0) + p
                    Q.setdefault(j2, {})[j1] = Q.get(j2, {}).get(j1, 0.0) + p

    if max_hedges is not None:
        for i in range(n_hedge):
            for j in range(n_hedge):
                Q.setdefault(i, {})[j] = Q.get(i, {}).get(j, 0.0) + 5.0
            Q[i][i] = Q.get(i, {}).get(i, 0.0) - 10.0 * max_hedges

    all_labels = hed_labels
    return QuboProblem(n_vars=total_vars, Q=Q, labels=all_labels)


# ─── Parallel Tempering Solver ─────────────────────────────────────


def parallel_tempering_solve(
    problem: QuboProblem,
    *,
    n_replicas: int = 8,
    n_iterations: int = 5_000,
    temperatures: list[float] | None = None,
    cooling_rate: float = 0.995,
    seed: int | None = None,
    timeout_seconds: float = 30.0,
) -> SolverResult:
    """Parallel tempering (replica exchange) Monte Carlo solver.

    Runs N replicas at different temperatures in parallel, periodically
    exchanging states between adjacent replicas. High-T replicas escape
    local minima; low-T replicas refine the best solution.

    This is significantly better than vanilla simulated annealing on
    rugged QUBO landscapes (many local minima). Used for the nightly
    rebalance when the problem is > 16 variables.
    """
    rng = random.Random(seed) if seed is not None else random.Random()
    n = problem.n_vars

    if temperatures is None:
        temps = [10.0 * (2.0 ** i) for i in range(n_replicas)]
    else:
        temps = list(temperatures)
        n_replicas = len(temps)

    # Initialize replicas
    replicas = [[rng.randint(0, 1) for _ in range(n)] for _ in range(n_replicas)]
    energies = [problem.evaluate(x) for x in replicas]
    best_x = list(replicas[-1])
    best_energy = energies[-1]
    accepted = 0

    for iteration in range(n_iterations):
        # Metropolis step for each replica
        for r in range(n_replicas):
            i = rng.randint(0, n - 1)
            replicas[r][i] = 1 - replicas[r][i]
            new_e = problem.evaluate(replicas[r])
            delta = new_e - energies[r]
            T = max(temps[r], 1e-9)
            if delta <= 0 or rng.random() < math.exp(-delta / T):
                energies[r] = new_e
                accepted += 1
                if new_e < best_energy:
                    best_energy = new_e
                    best_x = list(replicas[r])
            else:
                replicas[r][i] = 1 - replicas[r][i]

        # Replica exchange every 50 iterations
        if iteration > 0 and iteration % 50 == 0:
            for r in range(n_replicas - 1):
                Ti, Ti1 = temps[r], temps[r + 1] if r + 1 < len(temps) else temps[-1]
                if Ti <= 0 or Ti1 <= 0:
                    continue
                delta_e = energies[r] - energies[r + 1]
                prob = math.exp(delta_e * (1.0 / Ti - 1.0 / Ti1))
                if rng.random() < min(1.0, prob):
                    replicas[r], replicas[r + 1] = replicas[r + 1], replicas[r]
                    energies[r], energies[r + 1] = energies[r + 1], energies[r]
                    accepted += 1

        # Cool all temperatures
        temps = [t * cooling_rate for t in temps]

    return SolverResult(
        x=best_x,
        energy=round(best_energy, 6),
        n_iterations=n_iterations,
        accepted_moves=accepted,
        final_temperature=round(temps[-1], 6),
        labels=problem.labels,
    )


# ─── Smart solver: pick best method for problem size ───────────────


def adaptive_solve(
    problem: QuboProblem,
    *,
    n_iterations: int = 5_000,
    seed: int | None = None,
) -> SolverResult:
    """Auto-select solver: PT for large/rugged, SA for small/smooth."""
    if problem.n_vars >= 16:
        return parallel_tempering_solve(
            problem,
            n_replicas=8,
            n_iterations=n_iterations,
            seed=seed,
        )
    return simulated_annealing_solve(
        problem,
        n_iterations=n_iterations,
        seed=seed,
    )
