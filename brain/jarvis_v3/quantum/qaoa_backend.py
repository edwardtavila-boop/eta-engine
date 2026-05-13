"""Qiskit QAOA backend (Wave-11, 2026-04-27).

Real Qiskit dispatch for QUBO problems via QAOA (Quantum Approximate
Optimization Algorithm). Maps QUBO -> Ising Hamiltonian, then runs
QAOA with p layers on a simulator (default) or IBM hardware (when
``ibm_token`` and ``ibm_backend`` are provided).

Optional dependency: ``qiskit``. When not installed, ``available()``
returns False and ``solve_with_qaoa()`` raises ImportError so the
caller (cloud_adapter) can fall back to classical SA.

Design:

  * ``available()``: probe qiskit installation
  * ``solve_with_qaoa()``: run QAOA, return (x_solution, energy)
  * ``run_qaoa_simulator()``: alias that explicitly uses local
    simulator (no IBM credentials needed)
  * Operator notes: QAOA on near-term hardware shines for problems
    of size 10-30 binary variables; below that classical SA is
    fast and accurate; above that, QAOA's noise resilience matters

The backend is production-safe around optional quantum dependencies:
when Qiskit is missing the caller gets an ImportError and can fall
back; when Qiskit's result object does not expose a usable best
measurement, this module recovers with a classical verifier instead
of fabricating an all-zero vector.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        QuboProblem,
        SolverResult,
    )

logger = logging.getLogger(__name__)


def available() -> bool:
    """Return True iff qiskit is importable."""
    try:
        import importlib

        return importlib.util.find_spec("qiskit") is not None
    except (ImportError, AttributeError):
        return False


def qubo_to_ising(problem: QuboProblem) -> tuple[list[list[float]], float]:
    """Convert QUBO to Ising Hamiltonian.

    QUBO: minimize x^T Q x where x in {0, 1}^n
    Ising: minimize sum h_i * z_i + sum J_ij * z_i * z_j where z in {-1, +1}^n

    Standard substitution: x_i = (1 - z_i) / 2.

    Returns (J_matrix, constant_offset). Diagonal h_i is encoded on
    J[i][i] for compactness.
    """
    n = problem.n_vars
    J: list[list[float]] = [[0.0] * n for _ in range(n)]  # noqa: N806
    offset = 0.0
    for i, row in problem.Q.items():
        for j, qij in row.items():
            if i == j:
                # Diagonal: x_i = (1 - z_i)/2 -> x_i^2 = x_i
                # Q_ii * x_i = Q_ii * (1 - z_i) / 2
                #            = Q_ii/2 - Q_ii/2 * z_i
                J[i][i] += -qij / 2.0
                offset += qij / 2.0
            else:
                # Off-diagonal: x_i x_j = (1 - z_i)(1 - z_j)/4
                #             = 1/4 - z_i/4 - z_j/4 + z_i z_j / 4
                # Q_ij contributes Q_ij/4 to J_ij,
                # -Q_ij/4 to h_i, -Q_ij/4 to h_j, Q_ij/4 to offset
                J[i][j] += qij / 4.0
                J[i][i] += -qij / 4.0
                J[j][j] += -qij / 4.0
                offset += qij / 4.0
    return J, offset


def solve_with_qaoa(
    problem: QuboProblem,
    *,
    p_layers: int = 2,
    max_iter: int = 100,
    seed: int = 42,
    use_simulator: bool = True,
) -> SolverResult:
    """Run QAOA on the given QUBO problem.

    Parameters:
      * p_layers -- QAOA depth (more layers = better approximation,
        but more noise on real hardware). p=2 is a sensible default
        for problems up to 20 variables.
      * max_iter -- classical optimizer iteration cap (COBYLA default)
      * seed -- RNG seed for reproducibility
      * use_simulator -- when True, runs locally on Aer simulator;
        when False, attempts to use IBM Quantum cloud (requires
        QISKIT_IBM_TOKEN env var)

    Raises ImportError if qiskit is not installed.
    """
    if not available():
        raise ImportError(
            "qiskit not installed; install with `pip install qiskit "
            "qiskit-aer qiskit-optimization` or use the classical_sa "
            "backend instead",
        )

    # Defer all qiskit imports to call-time so the module loads even
    # without qiskit installed.
    #
    # Qiskit 2.x replaced V1 primitives with V2. The simplest portable
    # path that works across Qiskit 1.x and 2.x is StatevectorEstimator
    # from qiskit.primitives (pure-classical, no Aer-specific path).
    # For larger problems, callers can swap in Aer's V2 primitives.
    try:
        from qiskit.quantum_info import SparsePauliOp
        from qiskit_algorithms import QAOA
        from qiskit_algorithms.optimizers import COBYLA
    except ImportError as exc:
        raise ImportError(
            f"qiskit submodule import failed ({exc}); ensure qiskit-aer and qiskit-algorithms are installed",
        ) from exc

    # Sampler selection (qiskit_algorithms 0.4+ QAOA takes a Sampler,
    # not an Estimator -- QAOA samples the optimal bitstring rather
    # than estimating an expectation value).
    #
    # We prefer ``qiskit.primitives.StatevectorSampler`` for the local
    # path because it accepts QAOA's parameterized ansatz directly
    # without needing a basis-gate transpilation pass. Aer's V2
    # SamplerV2 trips on "unknown instruction: QAOA" because QAOA
    # is a custom gate that needs decomposition.
    sampler = None
    sampler_kind = ""
    if use_simulator:
        from qiskit.primitives import StatevectorSampler

        sampler = StatevectorSampler(seed=seed)
        sampler_kind = "qiskit.primitives.StatevectorSampler"
    else:
        # Cloud path: try IBM Runtime; fall back to local simulator
        try:
            from qiskit_ibm_runtime import (
                QiskitRuntimeService,
            )
            from qiskit_ibm_runtime import (
                SamplerV2 as IBMSampler,
            )

            service = QiskitRuntimeService(channel="ibm_quantum")
            backend = service.least_busy(simulator=False)
            sampler = IBMSampler(mode=backend)
            sampler_kind = "qiskit_ibm_runtime.SamplerV2"
        except (ImportError, Exception) as exc:  # noqa: BLE001
            logger.warning(
                "qaoa: IBM cloud unavailable (%s); falling back to StatevectorSampler",
                exc,
            )
            from qiskit.primitives import StatevectorSampler

            sampler = StatevectorSampler(seed=seed)
            sampler_kind = "qiskit.primitives.StatevectorSampler (fallback)"
    logger.debug("qaoa: using sampler %s", sampler_kind)

    # 1. Convert QUBO -> Ising Hamiltonian
    J, offset = qubo_to_ising(problem)  # noqa: N806 -- standard Ising notation
    n = problem.n_vars

    # 2. Build SparsePauliOp from Ising J matrix
    pauli_terms: list[tuple[str, float]] = []
    for i in range(n):
        for j in range(n):
            coeff = J[i][j]
            if coeff == 0:
                continue
            if i == j:
                # Single-Z term: Z_i
                pauli = ["I"] * n
                pauli[i] = "Z"
                pauli_terms.append(("".join(reversed(pauli)), coeff))
            elif j > i:
                # Two-qubit ZZ term: Z_i Z_j (only count once)
                pauli = ["I"] * n
                pauli[i] = "Z"
                pauli[j] = "Z"
                pauli_terms.append(("".join(reversed(pauli)), 2.0 * coeff))

    if not pauli_terms:
        # Trivial: all zero -> minimum is x = 0 vector, energy = offset
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import SolverResult

        return SolverResult(
            x=[0] * n,
            energy=offset,
            n_iterations=0,
            accepted_moves=0,
            final_temperature=0.0,
            labels=problem.labels,
        )

    hamiltonian = SparsePauliOp.from_list(pauli_terms)

    # 3. Run QAOA -- sampler was selected above
    optimizer = COBYLA(maxiter=max_iter)
    qaoa = QAOA(sampler=sampler, optimizer=optimizer, reps=p_layers)
    result = qaoa.compute_minimum_eigenvalue(hamiltonian)

    # 4. Extract bitstring. Older/newer qiskit-algorithms releases differ
    # on whether ``best_measurement`` is populated. Never return a fabricated
    # all-zero vector; recover through the same deterministic classical QUBO
    # verifier used by the cloud adapter.
    x = _extract_best_measurement_x(result, n)
    fallback_result = None
    if x is None:
        fallback_result = _classical_recovery_solution(problem, max_iter=max_iter, seed=seed)
        x = fallback_result.x

    # 5. Compute QUBO energy from the recovered x
    qubo_energy = fallback_result.energy if fallback_result is not None else problem.evaluate(x)

    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import SolverResult

    return SolverResult(
        x=x,
        energy=round(qubo_energy, 6),
        n_iterations=(max_iter + fallback_result.n_iterations if fallback_result is not None else max_iter),
        accepted_moves=(fallback_result.accepted_moves if fallback_result is not None else max_iter),
        final_temperature=(fallback_result.final_temperature if fallback_result is not None else 0.0),
        labels=problem.labels,
    )


def run_qaoa_simulator(
    problem: QuboProblem,
    *,
    p_layers: int = 2,
    max_iter: int = 100,
    seed: int = 42,
) -> SolverResult:
    """Convenience wrapper: always uses the local Aer simulator."""
    return solve_with_qaoa(
        problem,
        p_layers=p_layers,
        max_iter=max_iter,
        seed=seed,
        use_simulator=True,
    )


def _extract_best_measurement_x(result: object, n: int) -> list[int] | None:
    best_measurement = getattr(result, "best_measurement", None)
    if not best_measurement:
        return None
    bitstr = best_measurement.get("bitstring") if isinstance(best_measurement, dict) else None
    if not isinstance(bitstr, str):
        return None
    bits = [char for char in bitstr if char in {"0", "1"}]
    if not bits:
        return None
    x = [int(bit) for bit in bits[:n]]
    if len(x) < n:
        x.extend([0] * (n - len(x)))
    return x


def _classical_recovery_solution(
    problem: QuboProblem,
    *,
    max_iter: int,
    seed: int,
    exact_cutoff: int = 20,
) -> SolverResult:
    """Return a deterministic classical recovery solution for QAOA edge cases.

    Exact enumeration is cheap and preferable for the 10-20 variable QUBOs this
    layer normally sends to local QAOA. Larger instances fall back to the
    standard simulated annealer so recovery remains bounded.
    """
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        SolverResult,
        simulated_annealing_solve,
    )

    n = problem.n_vars
    if n > exact_cutoff:
        return simulated_annealing_solve(problem, n_iterations=max_iter, seed=seed)

    best_x = [0] * n
    best_energy = problem.evaluate(best_x)
    evaluations = 1
    for mask in range(1, 1 << n):
        x = [(mask >> i) & 1 for i in range(n)]
        energy = problem.evaluate(x)
        evaluations += 1
        if energy < best_energy:
            best_energy = energy
            best_x = x

    return SolverResult(
        x=best_x,
        energy=round(best_energy, 6),
        n_iterations=evaluations,
        accepted_moves=0,
        final_temperature=0.0,
        labels=problem.labels,
    )
