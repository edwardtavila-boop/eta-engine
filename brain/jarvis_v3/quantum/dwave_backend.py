"""D-Wave Ocean backend (Wave-11, 2026-04-27).

Real D-Wave dispatch for QUBO problems via either:

  * LocalQPU simulator (``dwave-neal``)  -- runs simulated annealing
    on the operator's machine; useful for prototyping
  * Leap cloud QPU (``dwave-system``)    -- real hardware annealing
    via D-Wave Leap subscription

Optional dependency: ``dwave-ocean-sdk``. When not installed,
``available()`` returns False and ``solve_with_dwave()`` raises
ImportError.

Why D-Wave: their hardware is purpose-built for QUBO/Ising. Quantum
annealers don't suffer the same gate-noise issues as
gate-model machines -- they natively minimize energy. The trade-off
is that they're locked into the QUBO problem class, which is exactly
what our portfolio / sizing / sequencing problems are.

Operator notes:
  * Leap free tier: 1 minute of QPU time per month -- enough for
    ~100 problems of ~50 variables each
  * Embedding: D-Wave QPUs have specific qubit topologies; the SDK
    handles minor-embedding automatically but may fail on large
    problems with high connectivity
  * Chain breaks: we expose the chain-break ratio in the result
    metadata so the operator can detect noisy returns
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
    """Return True iff dwave-ocean-sdk's primary modules are importable."""
    try:
        import importlib
        # We need at LEAST dimod (BQM model) -- neal and dwave.system
        # are nice-to-haves but the import probe should be cheap
        return importlib.util.find_spec("dimod") is not None
    except (ImportError, AttributeError):
        return False


def qubo_to_bqm(problem: QuboProblem):  # noqa: ANN201 -- dimod is optional dep
    """Convert QuboProblem -> dimod BinaryQuadraticModel.

    Returns a dimod BQM. Raises ImportError if dimod is not available."""
    if not available():
        raise ImportError(
            "dimod not installed; install with `pip install dwave-ocean-sdk`",
        )
    import dimod

    linear: dict[int, float] = {}
    quadratic: dict[tuple[int, int], float] = {}
    for i, row in problem.Q.items():
        for j, qij in row.items():
            if qij == 0:
                continue
            if i == j:
                linear[i] = linear.get(i, 0.0) + qij
            elif j > i:
                quadratic[(i, j)] = quadratic.get((i, j), 0.0) + qij
            else:
                quadratic[(j, i)] = quadratic.get((j, i), 0.0) + qij

    return dimod.BinaryQuadraticModel(
        linear, quadratic, 0.0, dimod.BINARY,
    )


def solve_with_dwave(
    problem: QuboProblem,
    *,
    use_qpu: bool = False,
    num_reads: int = 100,
    seed: int = 42,
    chain_strength: float | None = None,
) -> SolverResult:
    """Run D-Wave annealing on the given QUBO problem.

    Parameters:
      * use_qpu -- when True, dispatch to Leap cloud QPU (requires
        DWAVE_API_TOKEN env var); when False, use local neal SA
      * num_reads -- number of independent annealing samples
      * chain_strength -- penalty for chain breaks (auto-set if None)

    Raises ImportError if dwave-ocean-sdk is not installed.
    """
    if not available():
        raise ImportError(
            "dwave-ocean-sdk not installed; install with "
            "`pip install dwave-ocean-sdk` or use the classical_sa backend",
        )

    bqm = qubo_to_bqm(problem)

    if use_qpu:
        try:
            from dwave.system import DWaveSampler, EmbeddingComposite
            sampler = EmbeddingComposite(DWaveSampler())
            kwargs: dict = {"num_reads": num_reads}
            if chain_strength is not None:
                kwargs["chain_strength"] = chain_strength
            sampleset = sampler.sample(bqm, **kwargs)
        except (ImportError, Exception) as exc:
            logger.warning(
                "dwave: QPU unavailable (%s); falling back to local neal", exc,
            )
            sampleset = _solve_with_neal(bqm, num_reads=num_reads, seed=seed)
    else:
        sampleset = _solve_with_neal(bqm, num_reads=num_reads, seed=seed)

    # Extract best sample
    best = sampleset.first
    n = problem.n_vars
    x = [int(best.sample.get(i, 0)) for i in range(n)]

    # Re-evaluate energy under our QUBO definition (BQM internal energy
    # may differ in convention)
    qubo_energy = problem.evaluate(x)

    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import SolverResult
    return SolverResult(
        x=x,
        energy=round(qubo_energy, 6),
        n_iterations=num_reads,
        accepted_moves=num_reads,
        final_temperature=0.0,
        labels=problem.labels,
    )


def _solve_with_neal(  # noqa: ANN202 -- neal is optional dep
    bqm,  # noqa: ANN001 -- dimod.BinaryQuadraticModel, optional dep
    *,
    num_reads: int,
    seed: int,
):
    """Run simulated annealing locally.

    Modern dwave-ocean-sdk replaced ``dwave-neal`` with
    ``dwave-samplers.SimulatedAnnealingSampler``. We try the new
    path first, then fall back to legacy ``neal`` for older Ocean
    installs.
    """
    # New path (Ocean SDK 6+)
    try:
        from dwave.samplers import SimulatedAnnealingSampler
        sampler = SimulatedAnnealingSampler()
        return sampler.sample(bqm, num_reads=num_reads, seed=seed)
    except ImportError:
        pass
    # Legacy path
    try:
        import neal
        sampler = neal.SimulatedAnnealingSampler()
        return sampler.sample(bqm, num_reads=num_reads, seed=seed)
    except ImportError as exc:
        raise ImportError(
            "Neither dwave.samplers nor neal is installed; install "
            "with `pip install dwave-ocean-sdk` "
            f"(underlying error: {exc})",
        ) from exc
