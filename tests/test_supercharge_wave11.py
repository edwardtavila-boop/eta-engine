"""Tests for wave-11 (cloud quantum + tensor world-model + orchestrator).

Wave-11 finishes the supercharge by:
  * Wiring real Qiskit + D-Wave backends behind optional imports
  * Adding Tucker-decomposition tensor-network world model
  * Building JarvisOrchestrator that consults every layer
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── QAOA backend ─────────────────────────────────────────────────


def test_qaoa_backend_available_does_not_raise() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qaoa_backend import available

    # Returns bool either way; we don't have qiskit installed in CI
    assert isinstance(available(), bool)


def test_qubo_to_ising_converts_diagonal_only() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qaoa_backend import qubo_to_ising
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    p = QuboProblem.from_matrix([[2.0, 0.0], [0.0, -1.0]])
    J, offset = qubo_to_ising(p)  # noqa: N806 -- standard Ising notation
    # x_0 -> coef 2:  J[0][0] += -2/2 = -1; offset += 2/2 = 1
    # x_1 -> coef -1: J[1][1] += +1/2;     offset += -1/2
    assert J[0][0] == -1.0
    assert J[1][1] == 0.5
    assert offset == 0.5


def test_solve_with_qaoa_raises_if_qiskit_missing() -> None:
    import pytest

    from eta_engine.brain.jarvis_v3.quantum.qaoa_backend import (
        available,
        solve_with_qaoa,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    if available():
        pytest.skip("qiskit installed; skip the missing-import test")
    p = QuboProblem.from_matrix([[1.0, 0.0], [0.0, 1.0]])
    with pytest.raises(ImportError):
        solve_with_qaoa(p)


def test_qaoa_recovery_finds_nonzero_optimum_without_best_measurement() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qaoa_backend import (
        _classical_recovery_solution,
        _extract_best_measurement_x,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    p = QuboProblem.from_matrix([[-2.0, 0.0], [0.0, -3.0]], labels=["mnq", "btc"])
    result = _classical_recovery_solution(p, max_iter=50, seed=7)

    assert _extract_best_measurement_x(object(), 2) is None
    assert result.x == [1, 1]
    assert result.energy == -5.0
    assert result.selected_labels() == ["mnq", "btc"]


# ─── D-Wave backend ───────────────────────────────────────────────


def test_dwave_backend_available_returns_bool() -> None:
    from eta_engine.brain.jarvis_v3.quantum.dwave_backend import available

    assert isinstance(available(), bool)


def test_solve_with_dwave_raises_if_dimod_missing() -> None:
    import pytest

    from eta_engine.brain.jarvis_v3.quantum.dwave_backend import (
        available,
        solve_with_dwave,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    if available():
        pytest.skip("dimod installed; skip the missing-import test")
    p = QuboProblem.from_matrix([[1.0, 0.0], [0.0, 1.0]])
    with pytest.raises(ImportError):
        solve_with_dwave(p)


# ─── Cloud adapter dispatch (post-wiring) ─────────────────────────


def test_cloud_adapter_dispatches_to_qiskit_falls_back_when_missing(
    tmp_path: Path,
) -> None:
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        QuantumBackend,
        QuantumCloudAdapter,
        _ResultCache,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    adapter = QuantumCloudAdapter(
        cfg=CloudConfig(
            enable_cloud=True,
            preferred_backend=QuantumBackend.QISKIT,
            classical_validate_cloud=False,
        ),
        jobs_log_path=tmp_path / "jobs.jsonl",
        result_cache=_ResultCache(path=tmp_path / "cache.json"),
    )
    p = QuboProblem.from_matrix([[-2.0, 0.0], [0.0, -3.0]])
    result, record = adapter.solve(p, n_iterations=200, force_backend=QuantumBackend.QISKIT)
    # When qiskit is not installed, expect fell_back_to_classical=True
    from eta_engine.brain.jarvis_v3.quantum.qaoa_backend import available

    if not available():
        assert record.fell_back_to_classical is True


def test_cloud_adapter_dispatches_to_dwave_falls_back_when_missing(
    tmp_path: Path,
) -> None:
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        QuantumBackend,
        QuantumCloudAdapter,
        _ResultCache,
    )
    from eta_engine.brain.jarvis_v3.quantum.dwave_backend import available
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    adapter = QuantumCloudAdapter(
        cfg=CloudConfig(
            enable_cloud=True,
            preferred_backend=QuantumBackend.DWAVE,
            classical_validate_cloud=False,
        ),
        jobs_log_path=tmp_path / "jobs.jsonl",
        result_cache=_ResultCache(path=tmp_path / "cache.json"),
    )
    p = QuboProblem.from_matrix([[-1.0, 0.0], [0.0, -1.0]])
    result, record = adapter.solve(p, n_iterations=200, force_backend=QuantumBackend.DWAVE)
    if not available():
        assert record.fell_back_to_classical is True


def test_cloud_adapter_cross_check_updates_log_and_cache(tmp_path: Path, monkeypatch) -> None:
    import json

    from eta_engine.brain.jarvis_v3.quantum import qubo_solver as qubo_mod
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        QuantumBackend,
        QuantumCloudAdapter,
        _ResultCache,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        QuboProblem,
        SolverResult,
    )

    adapter = QuantumCloudAdapter(
        cfg=CloudConfig(
            enable_cloud=True,
            preferred_backend=QuantumBackend.QISKIT,
            classical_validate_cloud=True,
        ),
        jobs_log_path=tmp_path / "jobs.jsonl",
        result_cache=_ResultCache(path=tmp_path / "cache.json"),
    )
    p = QuboProblem.from_matrix([[-1.0, 0.0], [0.0, -1.0]], labels=["a", "b"])
    cloud_result = SolverResult(
        x=[0, 0],
        energy=0.0,
        n_iterations=10,
        accepted_moves=0,
        final_temperature=0.0,
        labels=["a", "b"],
    )
    classical_result = SolverResult(
        x=[1, 1],
        energy=-2.0,
        n_iterations=10,
        accepted_moves=0,
        final_temperature=0.0,
        labels=["a", "b"],
    )

    def fake_dispatch(*, backend, problem, n_iterations):
        return cloud_result, False

    def fake_sa(problem, *, n_iterations=0, seed=None):
        return classical_result

    monkeypatch.setattr(adapter, "_dispatch", fake_dispatch)
    monkeypatch.setattr(qubo_mod, "simulated_annealing_solve", fake_sa)

    result1, record1 = adapter.solve(
        p,
        n_iterations=20,
        force_backend=QuantumBackend.QISKIT,
    )
    result2, record2 = adapter.solve(p, n_iterations=20)

    assert result1.energy == -2.0
    assert record1.backend == QuantumBackend.CLASSICAL_SA.value
    assert record1.fell_back_to_classical is True
    assert "cross-check" in record1.note
    assert record2.used_cache is True
    assert result2.energy == -2.0
    assert record2.backend == QuantumBackend.CLASSICAL_SA.value

    logged = [json.loads(line) for line in (tmp_path / "jobs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "cross-check" in logged[0]["note"]


def test_cloud_adapter_failed_dispatch_records_classical_zero_cost(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        QuantumBackend,
        QuantumCloudAdapter,
        _ResultCache,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        QuboProblem,
        SolverResult,
    )

    adapter = QuantumCloudAdapter(
        cfg=CloudConfig(
            enable_cloud=True,
            preferred_backend=QuantumBackend.DWAVE,
            classical_validate_cloud=False,
        ),
        jobs_log_path=tmp_path / "jobs.jsonl",
        result_cache=_ResultCache(path=tmp_path / "cache.json"),
    )
    p = QuboProblem.from_matrix([[-1.0]], labels=["x"])
    fallback_result = SolverResult(
        x=[1],
        energy=-1.0,
        n_iterations=5,
        accepted_moves=1,
        final_temperature=0.0,
        labels=["x"],
    )

    def fake_dispatch(*, backend, problem, n_iterations):
        return fallback_result, True

    monkeypatch.setattr(adapter, "_dispatch", fake_dispatch)

    result, record = adapter.solve(
        p,
        n_iterations=20,
        force_backend=QuantumBackend.DWAVE,
    )

    assert result.energy == -1.0
    assert record.backend == QuantumBackend.CLASSICAL_SA.value
    assert record.fell_back_to_classical is True
    assert record.cost_estimate_usd == 0.0
    assert adapter._daily_spend_usd == 0.0


# ─── Tensor-network world model ───────────────────────────────────


def test_tensor_world_model_fits_simple_transition() -> None:
    from eta_engine.brain.jarvis_v3.quantum.tensor_world_model import (
        TensorWorldModel,
    )

    # State 0 with action 0 always -> state 1; state 1 with action 0 -> state 0
    transitions = {
        0: {0: {1: 10}},
        1: {0: {0: 10}},
    }
    twm = TensorWorldModel(rank=2)
    twm.fit(transitions, n_iters=5)
    assert twm.state_dim == 2
    assert twm.action_dim == 1


def test_tensor_world_model_predict_normalizes_to_unity() -> None:
    from eta_engine.brain.jarvis_v3.quantum.tensor_world_model import (
        TensorWorldModel,
    )

    transitions = {
        0: {0: {1: 5, 2: 5}},
        1: {0: {2: 10}},
        2: {0: {0: 10}},
    }
    twm = TensorWorldModel(rank=2)
    twm.fit(transitions, n_iters=5)
    dist = twm.predict_next_distribution(state=0, action=0)
    assert dist
    assert abs(sum(dist.values()) - 1.0) < 1e-6


def test_tensor_world_model_unknown_state_returns_empty() -> None:
    from eta_engine.brain.jarvis_v3.quantum.tensor_world_model import (
        TensorWorldModel,
    )

    transitions = {0: {0: {1: 5}}}
    twm = TensorWorldModel(rank=2)
    twm.fit(transitions, n_iters=5)
    dist = twm.predict_next_distribution(state=999, action=0)
    assert dist == {}


def test_tensor_world_model_latent_distance_zero_for_same_state() -> None:
    from eta_engine.brain.jarvis_v3.quantum.tensor_world_model import (
        TensorWorldModel,
    )

    transitions = {0: {0: {1: 5}}, 1: {0: {0: 5}}}
    twm = TensorWorldModel(rank=2)
    twm.fit(transitions, n_iters=5)
    assert twm.latent_distance(0, 0) == 0.0


# ─── Orchestrator ────────────────────────────────────────────────


def test_orchestrator_returns_decision_packet(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.orchestrator import JarvisOrchestrator

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    orch = JarvisOrchestrator(memory=mem, use_iterative_debate=False)
    p = Proposal(
        signal_id="orch1",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.3,
        sentiment=0.4,
        sage_score=0.5,
        slippage_bps_estimate=2.0,
    )
    packet = orch.deliberate(proposal=p, current_narrative="EMA stack aligned")
    assert packet.proposal_id == "orch1"
    assert packet.final_action in {"APPROVE_FULL", "APPROVE_HALF", "DEFER", "DENY"}
    assert 0.0 <= packet.final_size_multiplier <= 1.0
    assert 0.0 <= packet.confidence <= 1.0


def test_orchestrator_audit_record_serializable(tmp_path: Path) -> None:
    import json

    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.orchestrator import JarvisOrchestrator

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    orch = JarvisOrchestrator(memory=mem)
    p = Proposal(
        signal_id="orch2",
        direction="short",
        regime="bearish_low_vol",
        session="rth",
        stress=0.4,
        sentiment=-0.3,
        sage_score=0.4,
    )
    packet = orch.deliberate(proposal=p, current_narrative="bearish reversal")
    rec = packet.to_audit_record()
    s = json.dumps(rec)
    assert rec["policy_authority"] == "JARVIS"
    assert isinstance(rec["decision_seed"], int)
    assert rec["raw"]["proposal"]["sage_score"] == 0.4
    assert "rag" in s
    assert "causal" in s
    assert "world_model" in s
    assert "firm_board" in s


def test_orchestrator_high_stress_yields_deny_or_defer(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.orchestrator import JarvisOrchestrator

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    orch = JarvisOrchestrator(memory=mem)
    p = Proposal(
        signal_id="orch3",
        direction="long",
        regime="bearish_high_vol",
        session="overnight",
        stress=0.85,
        sentiment=0.0,
        sage_score=0.1,
        slippage_bps_estimate=15.0,  # high impact
    )
    packet = orch.deliberate(proposal=p)
    assert packet.final_action in {"DENY", "DEFER"}
    assert packet.final_size_multiplier == 0.0


def test_orchestrator_quantum_layer_runs_when_enabled(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.orchestrator import JarvisOrchestrator

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    orch = JarvisOrchestrator(memory=mem, consult_quantum=True)
    p = Proposal(
        signal_id="orch4",
        direction="long",
        regime="neutral",
        session="rth",
        stress=0.4,
        sentiment=0.3,
        sage_score=0.4,
    )
    packet = orch.deliberate(proposal=p, current_narrative="neutral entry")
    assert packet.quantum_used is True
    # Quantum contribution summary is non-empty when the layer ran
    assert packet.quantum_contribution_summary != ""


def test_orchestrator_iterative_debate_is_replayable(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.firm_board import Proposal
    from eta_engine.brain.jarvis_v3.memory_hierarchy import HierarchicalMemory
    from eta_engine.brain.jarvis_v3.orchestrator import JarvisOrchestrator

    mem = HierarchicalMemory(
        episodic_path=tmp_path / "ep.jsonl",
        semantic_path=tmp_path / "sem.json",
        procedural_path=tmp_path / "proc.jsonl",
    )
    orch = JarvisOrchestrator(memory=mem, use_iterative_debate=True)
    p = Proposal(
        signal_id="orch-replay",
        direction="long",
        regime="bullish_low_vol",
        session="rth",
        stress=0.25,
        sentiment=0.5,
        sage_score=0.7,
        slippage_bps_estimate=1.5,
    )

    first = orch.deliberate(proposal=p, current_narrative="clean trend entry")
    second = orch.deliberate(proposal=p, current_narrative="clean trend entry")

    assert first.decision_seed == second.decision_seed
    assert first.final_action == second.final_action
    assert first.final_size_multiplier == second.final_size_multiplier
    assert first.confidence == second.confidence
    assert first.to_audit_record()["raw"]["proposal"] == second.to_audit_record()["raw"]["proposal"]
