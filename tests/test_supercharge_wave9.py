"""Tests for wave-9 (quantum-hybrid layer, 2026-04-27).

Covers:
  * QUBO solver primitive + simulated annealing
  * portfolio_allocation_qubo encoder
  * sizing_basket_qubo encoder
  * Tensor-network signal selector (diversity-aware greedy)
  * Cloud adapter (with classical_sa fallback)
  * QuantumOptimizerAgent (firm-board pluggable)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── QUBO solver ──────────────────────────────────────────────────


def test_qubo_problem_evaluate_diagonal_only() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    p = QuboProblem.from_matrix([[2.0, 0.0], [0.0, -1.0]])
    # x = [1, 0] -> 2*1 + 0 = 2
    assert p.evaluate([1, 0]) == 2.0
    # x = [0, 1] -> 0 + (-1) = -1
    assert p.evaluate([0, 1]) == -1.0


def test_qubo_problem_evaluate_with_off_diagonal() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    # Q = [[0, -2], [-2, 0]]; x^T Q x = -4*x0*x1
    p = QuboProblem.from_matrix([[0.0, -2.0], [-2.0, 0.0]])
    assert p.evaluate([1, 1]) == -4.0
    assert p.evaluate([1, 0]) == 0.0


def test_simulated_annealing_finds_good_solution_on_easy_problem() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        QuboProblem,
        simulated_annealing_solve,
    )

    # Minimum is x = [1, 1] with energy -4
    p = QuboProblem.from_matrix(
        [[-1.0, -1.0], [-1.0, -1.0]],
        labels=["a", "b"],
    )
    result = simulated_annealing_solve(p, n_iterations=2_000, seed=42)
    assert result.x == [1, 1]
    assert result.energy <= -3.5
    assert result.selected_labels() == ["a", "b"]


def test_simulated_annealing_returns_best_seen_not_final() -> None:
    """Even when annealing wanders away from optimum at the end, the
    best-seen state should be returned."""
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        QuboProblem,
        simulated_annealing_solve,
    )

    p = QuboProblem.from_matrix([[-5.0, 0.0], [0.0, -3.0]])
    result = simulated_annealing_solve(p, n_iterations=500, seed=1)
    # Optimum is [1, 1] with energy -8
    assert result.energy <= -7.0


def test_portfolio_allocation_qubo_picks_high_return_low_risk() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        portfolio_allocation_qubo,
        simulated_annealing_solve,
    )

    # Asset 0: high return, low variance -> should be picked
    # Asset 1: low return, high variance -> should be skipped
    p = portfolio_allocation_qubo(
        expected_returns=[3.0, 0.1],
        covariance=[[0.1, 0.0], [0.0, 5.0]],
        risk_aversion=1.0,
        asset_labels=["good", "bad"],
    )
    r = simulated_annealing_solve(p, n_iterations=2_000, seed=42)
    assert "good" in r.selected_labels()


def test_portfolio_allocation_qubo_respects_cardinality() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        portfolio_allocation_qubo,
        simulated_annealing_solve,
    )

    p = portfolio_allocation_qubo(
        expected_returns=[1.0, 1.0, 1.0, 1.0],
        covariance=[
            [0.1, 0.0, 0.0, 0.0],
            [0.0, 0.1, 0.0, 0.0],
            [0.0, 0.0, 0.1, 0.0],
            [0.0, 0.0, 0.0, 0.1],
        ],
        risk_aversion=1.0,
        cardinality_min=2,
        cardinality_max=2,
        cardinality_penalty=10.0,
    )
    r = simulated_annealing_solve(p, n_iterations=3_000, seed=42)
    # Should pick exactly 2 assets
    assert sum(r.x) == 2


def test_sizing_basket_qubo_picks_highest_score_signals() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        simulated_annealing_solve,
        sizing_basket_qubo,
    )

    p = sizing_basket_qubo(
        expected_r=[3.0, 0.5, 2.5, 0.3],
        pairwise_correlation=[
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        correlation_penalty=0.0,
        max_picks=2,
        pick_penalty=10.0,
        signal_labels=["s0", "s1", "s2", "s3"],
    )
    r = simulated_annealing_solve(p, n_iterations=2_000, seed=42)
    selected = set(r.selected_labels())
    # Top 2 by raw score are s0 and s2
    assert selected == {"s0", "s2"}


def test_sizing_basket_qubo_correlation_penalty_diversifies() -> None:
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
        simulated_annealing_solve,
        sizing_basket_qubo,
    )

    # s0 and s1 are perfectly correlated; s2 is uncorrelated
    p = sizing_basket_qubo(
        expected_r=[2.0, 2.0, 1.5],
        pairwise_correlation=[
            [1.0, 0.95, 0.0],
            [0.95, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        correlation_penalty=10.0,
        max_picks=2,
        pick_penalty=5.0,
        signal_labels=["s0", "s1", "s2"],
    )
    r = simulated_annealing_solve(p, n_iterations=3_000, seed=42)
    selected = set(r.selected_labels())
    # Should NOT pick both s0 and s1 (correlated)
    assert selected != {"s0", "s1"}


# ─── Tensor network selector ──────────────────────────────────────


def test_select_top_signal_combination_handles_empty() -> None:
    from eta_engine.brain.jarvis_v3.quantum.tensor_network import (
        select_top_signal_combination,
    )

    out = select_top_signal_combination([], k=3)
    assert out.selected == []
    assert out.objective == 0.0


def test_select_top_signal_combination_returns_all_when_k_exceeds_n() -> None:
    from eta_engine.brain.jarvis_v3.quantum.tensor_network import (
        SignalScore,
        select_top_signal_combination,
    )

    cands = [
        SignalScore(name="a", score=0.5, features=[1, 0]),
        SignalScore(name="b", score=0.4, features=[0, 1]),
    ]
    out = select_top_signal_combination(cands, k=5)
    assert len(out.selected) == 2


def test_select_top_signal_combination_diversifies_picks() -> None:
    from eta_engine.brain.jarvis_v3.quantum.tensor_network import (
        SignalScore,
        select_top_signal_combination,
    )

    # 4 candidates: 2 nearly identical (high overlap) vs 2 orthogonal
    cands = [
        SignalScore(name="dup_a", score=0.8, features=[1, 1, 0, 0]),
        SignalScore(name="dup_b", score=0.79, features=[1, 1, 0, 0]),
        SignalScore(name="orth_a", score=0.6, features=[0, 0, 1, 0]),
        SignalScore(name="orth_b", score=0.5, features=[0, 0, 0, 1]),
    ]
    out = select_top_signal_combination(cands, k=2, diversity_weight=0.5)
    names = {s.name for s in out.selected}
    # First pick: "dup_a" (highest score)
    # Second pick: should NOT be dup_b (overlap=1) -> should be orth_a
    assert "dup_a" in names
    assert "dup_b" not in names


def test_signal_correlation_matrix_is_symmetric_with_unit_diagonal() -> None:
    from eta_engine.brain.jarvis_v3.quantum.tensor_network import (
        SignalScore,
        signal_correlation_matrix,
    )

    cands = [
        SignalScore(name="a", score=0, features=[1, 0, 0]),
        SignalScore(name="b", score=0, features=[0, 1, 0]),
        SignalScore(name="c", score=0, features=[1, 1, 0]),
    ]
    M = signal_correlation_matrix(cands)  # noqa: N806 -- standard matrix notation
    assert len(M) == 3
    # Diagonals are 1.0 (cosine of vec with itself)
    for i in range(3):
        assert abs(M[i][i] - 1.0) < 1e-9
    # Symmetry
    for i in range(3):
        for j in range(3):
            assert abs(M[i][j] - M[j][i]) < 1e-9


# ─── Cloud adapter ────────────────────────────────────────────────


def test_cloud_adapter_falls_back_to_classical_when_cloud_disabled(
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
        cfg=CloudConfig(enable_cloud=False),
        jobs_log_path=tmp_path / "jobs.jsonl",
        result_cache=_ResultCache(path=tmp_path / "cache.json"),
    )
    p = QuboProblem.from_matrix([[-2.0, 0.0], [0.0, -3.0]], labels=["x", "y"])
    result, record = adapter.solve(p, n_iterations=500)
    assert record.backend == QuantumBackend.CLASSICAL_SA.value
    assert record.fell_back_to_classical is False
    assert result.energy <= -4.0


def test_cloud_adapter_caches_repeat_calls(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        QuantumCloudAdapter,
        _ResultCache,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    adapter = QuantumCloudAdapter(
        cfg=CloudConfig(enable_cloud=False),
        jobs_log_path=tmp_path / "jobs.jsonl",
        result_cache=_ResultCache(path=tmp_path / "cache.json"),
    )
    p = QuboProblem.from_matrix([[-1.0, 0.0], [0.0, -1.0]])
    _, rec1 = adapter.solve(p, n_iterations=500)
    _, rec2 = adapter.solve(p, n_iterations=500)
    assert rec1.used_cache is False
    assert rec2.used_cache is True


def test_cloud_adapter_cache_rebinds_labels_to_current_problem(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        QuantumCloudAdapter,
        _ResultCache,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    adapter = QuantumCloudAdapter(
        cfg=CloudConfig(enable_cloud=False),
        jobs_log_path=tmp_path / "jobs.jsonl",
        result_cache=_ResultCache(path=tmp_path / "cache.json"),
    )
    p1 = QuboProblem.from_matrix(
        [[-1.0, 0.0], [0.0, -1.0]],
        labels=["alpha", "beta"],
    )
    p2 = QuboProblem.from_matrix(
        [[-1.0, 0.0], [0.0, -1.0]],
        labels=["omega", "sigma"],
    )

    adapter.solve(p1, n_iterations=500)
    result2, rec2 = adapter.solve(p2, n_iterations=500)

    assert rec2.used_cache is True
    assert result2.labels == ["omega", "sigma"]
    assert result2.selected_labels() == ["omega", "sigma"]


def test_cloud_adapter_appends_to_jobs_log(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        QuantumCloudAdapter,
        _ResultCache,
    )
    from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

    log = tmp_path / "jobs.jsonl"
    adapter = QuantumCloudAdapter(
        cfg=CloudConfig(enable_cloud=False),
        jobs_log_path=log,
        result_cache=_ResultCache(path=tmp_path / "cache.json"),
    )
    p = QuboProblem.from_matrix([[1.0, 0.0], [0.0, 1.0]])
    adapter.solve(p, n_iterations=500)
    assert log.exists()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_cloud_adapter_available_backends_always_includes_classical() -> None:
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        QuantumBackend,
        QuantumCloudAdapter,
    )

    adapter = QuantumCloudAdapter()
    backs = adapter.available_backends()
    assert QuantumBackend.CLASSICAL_SA in backs


# ─── QuantumOptimizerAgent ────────────────────────────────────────


def test_quantum_agent_allocate_portfolio_returns_recommendation(
    tmp_path: Path,
) -> None:
    from eta_engine.brain.jarvis_v3.quantum import (
        QuantumCloudAdapter,
        QuantumOptimizerAgent,
    )
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        _ResultCache,
    )

    agent = QuantumOptimizerAgent(
        adapter=QuantumCloudAdapter(
            cfg=CloudConfig(enable_cloud=False),
            jobs_log_path=tmp_path / "jobs.jsonl",
            result_cache=_ResultCache(path=tmp_path / "cache.json"),
        ),
        n_iterations=1_500,
    )
    rec = agent.allocate_portfolio(
        symbols=["MNQ", "BTC"],
        expected_returns=[2.0, 1.0],
        covariance=[[0.1, 0.0], [0.0, 0.5]],
        risk_aversion=1.0,
    )
    assert rec.kind.value == "PORTFOLIO_ALLOCATION"
    assert "Quantum-optimizer" in rec.contribution_summary
    assert rec.n_vars == 2


def test_quantum_agent_select_signal_basket_qubo_path(tmp_path: Path) -> None:
    from eta_engine.brain.jarvis_v3.quantum import (
        QuantumCloudAdapter,
        QuantumOptimizerAgent,
        SignalScore,
    )
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        _ResultCache,
    )

    agent = QuantumOptimizerAgent(
        adapter=QuantumCloudAdapter(
            cfg=CloudConfig(enable_cloud=False),
            jobs_log_path=tmp_path / "jobs.jsonl",
            result_cache=_ResultCache(path=tmp_path / "cache.json"),
        ),
        n_iterations=2_000,
    )
    cands = [
        SignalScore(name="wyckoff", score=2.0, features=[1, 0, 0]),
        SignalScore(name="fib_618", score=1.5, features=[0, 1, 0]),
        SignalScore(name="liquidity", score=0.5, features=[0, 0, 1]),
    ]
    rec = agent.select_signal_basket(
        candidates=cands,
        max_picks=2,
        correlation_penalty=0.5,
        use_qubo=True,
    )
    assert rec.kind.value == "SIZING_BASKET"
    assert len(rec.selected_labels) == 2


def test_quantum_agent_select_signal_basket_greedy_path() -> None:
    from eta_engine.brain.jarvis_v3.quantum import (
        QuantumOptimizerAgent,
        SignalScore,
    )

    agent = QuantumOptimizerAgent()
    cands = [
        SignalScore(name="a", score=0.9, features=[1, 0]),
        SignalScore(name="b", score=0.8, features=[0, 1]),
        SignalScore(name="c", score=0.7, features=[1, 1]),
    ]
    rec = agent.select_signal_basket(
        candidates=cands,
        max_picks=2,
        use_qubo=False,
    )
    assert rec.backend_used == "tensor_network_greedy"
    assert len(rec.selected_labels) == 2


def test_quantum_agent_sequence_orders_picks_low_impact(
    tmp_path: Path,
) -> None:
    from eta_engine.brain.jarvis_v3.quantum import (
        QuantumCloudAdapter,
        QuantumOptimizerAgent,
    )
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig,
        _ResultCache,
    )

    agent = QuantumOptimizerAgent(
        adapter=QuantumCloudAdapter(
            cfg=CloudConfig(enable_cloud=False),
            jobs_log_path=tmp_path / "jobs.jsonl",
            result_cache=_ResultCache(path=tmp_path / "cache.json"),
        ),
        n_iterations=2_000,
    )
    rec = agent.sequence_orders(
        order_labels=["small", "medium", "large"],
        impact_estimates_bps=[1.0, 5.0, 25.0],
        adjacency_penalty_bps=2.0,
    )
    assert rec.kind.value == "EXECUTION_SEQUENCING"
    # The objective minimizes total impact, so picking only "small"
    # (or empty) should be preferred over picking "large"
    assert "large" not in rec.selected_labels
