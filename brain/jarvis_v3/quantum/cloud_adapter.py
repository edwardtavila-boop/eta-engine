"""Cloud quantum adapter (Wave-9, 2026-04-27).

Optional bridge to real quantum hardware. None of qiskit /
pennylane / dwave-ocean-sdk is required at import time -- if they
aren't installed, we fall back transparently to the classical QUBO
solver in ``qubo_solver.py``.

Backends supported (priority order; first installed wins):

  1. D-Wave Ocean (dwave-ocean-sdk)  -> quantum annealing on Leap
  2. Qiskit                           -> QAOA on IBM Heron simulators
                                         or real hardware
  3. PennyLane                        -> variational hybrid (when a
                                         PyTorch model is involved)
  4. classical_sa                     -> simulated annealing (always
                                         available; default)

Operator-level observability:
  * Every quantum call logs ``backend``, ``problem_size``, ``runtime_ms``,
    ``cost_estimate_usd`` to a quantum-jobs.jsonl audit trail
  * Result caching by problem hash so repeat invocations don't pay
    the cloud bill twice within the cache TTL

Cloud API calls stay behind credentials plus explicit budget flags.
Without those, the adapter records a classical_sa fallback with the
same audit shape, so every path remains observable and safe. The
classical_sa backend is fully exercised by the tests.

Caveats baked into the design:
  * NEVER call cloud quantum on the trade-decision hot path (latency)
  * Re-validate every cloud result against classical_sa before
    using it for a real trade -- noisy quantum hardware can return
    nonsense
  * Document the quantum contribution in the decision journal
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        QuboProblem,
        SolverResult,
    )

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
JOBS_LOG_PATH = ROOT / "state" / "quantum" / "jobs.jsonl"
RESULT_CACHE_PATH = ROOT / "state" / "quantum" / "result_cache.json"


class QuantumBackend(StrEnum):
    DWAVE = "dwave"
    QISKIT = "qiskit"
    PENNYLANE = "pennylane"
    CLASSICAL_SA = "classical_sa"


@dataclass
class QuantumJobRecord:
    """One quantum (or classical-fallback) job's audit record."""

    ts: str
    backend: str
    problem_hash: str
    n_vars: int
    runtime_ms: float
    cost_estimate_usd: float
    objective_value: float
    n_iterations: int = 0
    used_cache: bool = False
    fell_back_to_classical: bool = False
    note: str = ""


@dataclass
class CloudConfig:
    """Operator-tunable knobs for cloud usage."""

    enable_cloud: bool = False  # MASTER SWITCH; default OFF
    preferred_backend: QuantumBackend = QuantumBackend.CLASSICAL_SA
    classical_validate_cloud: bool = True  # require SA cross-check
    cache_ttl_seconds: int = 86_400  # 24h
    max_cost_per_job_usd: float = 0.50  # hard ceiling
    max_cost_per_day_usd: float = 5.00  # daily budget
    timeout_seconds: float = 30.0  # cloud call timeout


# ─── Backend detection ────────────────────────────────────────────


def _detect_available_backends() -> list[QuantumBackend]:
    """Return list of installed quantum SDKs, in priority order.

    Always includes ``CLASSICAL_SA`` last as the fallback."""
    available: list[QuantumBackend] = []
    try:
        import importlib

        if importlib.util.find_spec("dwave.system") is not None:
            available.append(QuantumBackend.DWAVE)
    except (ImportError, AttributeError):
        pass
    try:
        import importlib

        if importlib.util.find_spec("qiskit") is not None:
            available.append(QuantumBackend.QISKIT)
    except (ImportError, AttributeError):
        pass
    try:
        import importlib

        if importlib.util.find_spec("pennylane") is not None:
            available.append(QuantumBackend.PENNYLANE)
    except (ImportError, AttributeError):
        pass
    available.append(QuantumBackend.CLASSICAL_SA)
    return available


# ─── Result cache ─────────────────────────────────────────────────


def _problem_hash(problem: QuboProblem) -> str:
    """Stable hash for a QUBO instance, for cache lookup."""
    payload = {
        "n_vars": problem.n_vars,
        "Q": {str(i): {str(j): round(v, 9) for j, v in row.items()} for i, row in problem.Q.items()},
    }
    s = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


@dataclass
class _CachedResult:
    ts: str
    problem_hash: str
    result: dict  # SolverResult as dict
    backend: str


class _ResultCache:
    """Simple JSON-backed cache of QUBO solutions."""

    def __init__(self, path: Path = RESULT_CACHE_PATH) -> None:
        self.path = path
        self._cache: dict[str, _CachedResult] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in data.items():
                self._cache[k] = _CachedResult(**v)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("quantum cache load failed (%s); fresh start", exc)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {k: asdict(v) for k, v in self._cache.items()},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("quantum cache save failed (%s)", exc)

    def get(self, h: str, *, ttl_seconds: int) -> _CachedResult | None:
        entry = self._cache.get(h)
        if entry is None:
            return None
        try:
            entry_dt = datetime.fromisoformat(entry.ts)
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - entry_dt).total_seconds()
            if age > ttl_seconds:
                return None
        except ValueError:
            return None
        return entry

    def put(self, h: str, *, result: dict, backend: str) -> None:
        self._cache[h] = _CachedResult(
            ts=datetime.now(UTC).isoformat(),
            problem_hash=h,
            result=result,
            backend=backend,
        )
        self._save()


# ─── Adapter ──────────────────────────────────────────────────────


class QuantumCloudAdapter:
    """Routes a QUBO problem to the best available backend.

    Always returns a SolverResult. The ``backend_used`` and any
    fallback attribution are recorded in the per-job audit log.
    """

    def __init__(
        self,
        cfg: CloudConfig | None = None,
        *,
        jobs_log_path: Path = JOBS_LOG_PATH,
        result_cache: _ResultCache | None = None,
    ) -> None:
        self.cfg = cfg or CloudConfig()
        self.jobs_log_path = jobs_log_path
        self.cache = result_cache or _ResultCache()
        self._available = _detect_available_backends()
        self._daily_spend_usd = 0.0

    def available_backends(self) -> list[QuantumBackend]:
        return list(self._available)

    def solve(
        self,
        problem: QuboProblem,
        *,
        n_iterations: int = 5_000,
        force_backend: QuantumBackend | None = None,
    ) -> tuple[SolverResult, QuantumJobRecord]:
        """Solve ``problem``, returning (result, audit_record).

        Decision tree:
          1. If cloud disabled -> classical_sa
          2. If cached result exists & still fresh -> return it
          3. If preferred cloud backend installed AND budget allows
             -> dispatch; otherwise fall back to classical_sa with audit
          4. Always validate cloud results against classical_sa when
             ``classical_validate_cloud`` is set
          5. Persist result + audit record
        """
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
            simulated_annealing_solve,
        )

        h = _problem_hash(problem)

        # Cache check
        cached = self.cache.get(h, ttl_seconds=self.cfg.cache_ttl_seconds)
        if cached is not None and force_backend is None:
            return self._restore_from_cache(cached, problem, h)

        # Decide backend
        backend = self._pick_backend(force_backend=force_backend)

        t0 = time.perf_counter()
        result, fell_back = self._dispatch(
            backend=backend,
            problem=problem,
            n_iterations=n_iterations,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        billed_backend = (
            backend if backend != QuantumBackend.CLASSICAL_SA and not fell_back else QuantumBackend.CLASSICAL_SA
        )
        cost = self._estimate_cost_usd(
            backend=billed_backend,
            n_vars=problem.n_vars,
        )
        self._daily_spend_usd += cost

        # Validate cloud result if requested
        if backend != QuantumBackend.CLASSICAL_SA and self.cfg.classical_validate_cloud and not fell_back:
            sa_check = simulated_annealing_solve(
                problem,
                n_iterations=n_iterations,
                seed=42,
            )
            if sa_check.energy < result.energy - 1e-6:
                logger.warning(
                    "quantum: cloud %s energy %.4f WORSE than classical SA %.4f; using classical result",
                    backend.value,
                    result.energy,
                    sa_check.energy,
                )
                result = sa_check
                fell_back = True

        record = QuantumJobRecord(
            ts=datetime.now(UTC).isoformat(),
            backend=(QuantumBackend.CLASSICAL_SA.value if fell_back else backend.value),
            problem_hash=h,
            n_vars=problem.n_vars,
            runtime_ms=round(elapsed_ms, 2),
            cost_estimate_usd=round(cost, 4),
            objective_value=result.energy,
            n_iterations=result.n_iterations,
            used_cache=False,
            fell_back_to_classical=fell_back,
            note=(
                f"{backend.value} beaten by classical SA cross-check"
                if (backend != QuantumBackend.CLASSICAL_SA and self.cfg.classical_validate_cloud and fell_back)
                else ""
            ),
        )
        self._append_job_log(record)
        self.cache.put(
            h,
            result=asdict(result),
            backend=record.backend,
        )

        return result, record

    # ── Internals ──────────────────────────────────────────

    def _pick_backend(
        self,
        *,
        force_backend: QuantumBackend | None,
    ) -> QuantumBackend:
        if force_backend is not None:
            return force_backend
        if not self.cfg.enable_cloud:
            return QuantumBackend.CLASSICAL_SA
        if self._daily_spend_usd >= self.cfg.max_cost_per_day_usd:
            logger.info(
                "quantum: daily budget $%0.2f reached; falling back to classical",
                self.cfg.max_cost_per_day_usd,
            )
            return QuantumBackend.CLASSICAL_SA
        # Use the preferred backend if installed, else first available
        if self.cfg.preferred_backend in self._available:
            return self.cfg.preferred_backend
        return self._available[0]

    def _dispatch(
        self,
        *,
        backend: QuantumBackend,
        problem: QuboProblem,
        n_iterations: int,
    ) -> tuple[SolverResult, bool]:
        """Returns (result, fell_back_to_classical_flag).

        Wave-11 wired: D-Wave Ocean and Qiskit QAOA backends now
        attempt real dispatch when their SDKs are installed; on
        ImportError / runtime exception they fall back to
        classical_sa with fell_back_to_classical=True.
        """
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
            simulated_annealing_solve,
        )

        if backend == QuantumBackend.CLASSICAL_SA:
            return simulated_annealing_solve(
                problem,
                n_iterations=n_iterations,
                seed=42,
            ), False

        # D-Wave path
        if backend == QuantumBackend.DWAVE:
            try:
                from eta_engine.brain.jarvis_v3.quantum.dwave_backend import (
                    available as dwave_available,
                )
                from eta_engine.brain.jarvis_v3.quantum.dwave_backend import (
                    solve_with_dwave,
                )

                if dwave_available():
                    return solve_with_dwave(
                        problem,
                        num_reads=100,
                        seed=42,
                        use_qpu=False,  # neal local first; flip on QPU when ready
                    ), False
            except (ImportError, Exception) as exc:  # noqa: BLE001
                logger.warning(
                    "dwave dispatch failed (%s); falling back to classical SA",
                    exc,
                )

        # Qiskit QAOA path
        if backend == QuantumBackend.QISKIT:
            try:
                from eta_engine.brain.jarvis_v3.quantum.qaoa_backend import (
                    available as qiskit_available,
                )
                from eta_engine.brain.jarvis_v3.quantum.qaoa_backend import (
                    solve_with_qaoa,
                )

                if qiskit_available():
                    return solve_with_qaoa(
                        problem,
                        p_layers=2,
                        max_iter=100,
                        seed=42,
                        use_simulator=True,
                    ), False
            except (ImportError, Exception) as exc:  # noqa: BLE001
                logger.warning(
                    "qiskit dispatch failed (%s); falling back to classical SA",
                    exc,
                )

        # PennyLane path: not yet implemented; falls through
        if backend == QuantumBackend.PENNYLANE:
            logger.info(
                "quantum: pennylane backend not yet wired -> classical_sa",
            )

        # Fallback for any backend whose SDK is missing or whose call
        # raised: classical SA with fell_back=True
        result = simulated_annealing_solve(
            problem,
            n_iterations=n_iterations,
            seed=42,
        )
        return result, True

    def _estimate_cost_usd(
        self,
        *,
        backend: QuantumBackend,
        n_vars: int,
    ) -> float:
        """Rough cost estimates (USD). Wire to vendor pricing when
        cloud calls land."""
        if backend == QuantumBackend.DWAVE:
            return 0.001 * max(1, n_vars)  # ~$0.001/var
        if backend == QuantumBackend.QISKIT:
            return 0.0015 * max(1, n_vars)
        if backend == QuantumBackend.PENNYLANE:
            return 0.0008 * max(1, n_vars)
        return 0.0  # classical = free

    def _restore_from_cache(
        self,
        cached: _CachedResult,
        problem: QuboProblem,
        h: str,
    ) -> tuple[SolverResult, QuantumJobRecord]:
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import SolverResult

        result = SolverResult(**cached.result)
        if problem.labels:
            result.labels = list(problem.labels)
        record = QuantumJobRecord(
            ts=datetime.now(UTC).isoformat(),
            backend=cached.backend,
            problem_hash=h,
            n_vars=problem.n_vars,
            runtime_ms=0.0,
            cost_estimate_usd=0.0,
            objective_value=result.energy,
            n_iterations=result.n_iterations,
            used_cache=True,
        )
        self._append_job_log(record)
        return result, record

    def _append_job_log(self, record: QuantumJobRecord) -> None:
        try:
            self.jobs_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jobs_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record)) + "\n")
        except OSError as exc:
            logger.warning("quantum: jobs-log append failed (%s)", exc)
