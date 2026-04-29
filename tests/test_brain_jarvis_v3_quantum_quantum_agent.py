from __future__ import annotations

from dataclasses import dataclass

from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import QuantumJobRecord
from eta_engine.brain.jarvis_v3.quantum.quantum_agent import (
    ProblemKind,
    QuantumOptimizerAgent,
)
from eta_engine.brain.jarvis_v3.quantum.qubo_solver import SolverResult
from eta_engine.brain.jarvis_v3.quantum.tensor_network import SignalScore


@dataclass
class _FakeAdapter:
    result: SolverResult
    backend: str = "classical_sa"

    def __post_init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    def solve(self, problem: object, *, n_iterations: int = 5_000) -> tuple[SolverResult, QuantumJobRecord]:
        self.calls.append((problem, n_iterations))
        self.result.labels = getattr(problem, "labels", [])
        return self.result, QuantumJobRecord(
            ts="2026-04-29T00:00:00+00:00",
            backend=self.backend,
            problem_hash="unit-test",
            n_vars=getattr(problem, "n_vars", len(self.result.x)),
            runtime_ms=12.5,
            cost_estimate_usd=0.0,
            objective_value=self.result.energy,
            n_iterations=n_iterations,
        )


def _solver_result(bits: list[int], *, energy: float = -1.25) -> SolverResult:
    return SolverResult(
        x=bits,
        energy=energy,
        n_iterations=7,
        accepted_moves=3,
        final_temperature=0.01,
    )


def test_allocate_portfolio_returns_auditable_recommendation() -> None:
    adapter = _FakeAdapter(_solver_result([1, 0, 1], energy=-2.5))
    agent = QuantumOptimizerAgent(adapter=adapter, n_iterations=77)

    recommendation = agent.allocate_portfolio(
        symbols=["MNQ", "BTC", "ETH"],
        expected_returns=[1.2, 0.5, 0.7],
        covariance=[
            [0.2, 0.01, 0.02],
            [0.01, 0.3, 0.04],
            [0.02, 0.04, 0.25],
        ],
        target_n_positions=2,
    )

    assert recommendation.kind is ProblemKind.PORTFOLIO_ALLOCATION
    assert recommendation.selected_labels == ["MNQ", "ETH"]
    assert recommendation.objective == -2.5
    assert recommendation.backend_used == "classical_sa"
    assert recommendation.n_vars == 3
    assert recommendation.raw_solution == [1, 0, 1]
    assert "selected 2/3 symbols" in recommendation.contribution_summary
    assert adapter.calls[0][1] == 77


def test_select_signal_basket_qubo_preserves_penalty_metadata() -> None:
    adapter = _FakeAdapter(_solver_result([0, 1, 1], energy=-1.75), backend="cache")
    agent = QuantumOptimizerAgent(adapter=adapter)
    candidates = [
        SignalScore(name="wyckoff", score=0.4, features=[1.0, 0.0]),
        SignalScore(name="orb", score=0.8, features=[0.0, 1.0]),
        SignalScore(name="liquidity", score=0.7, features=[0.2, 0.8]),
    ]

    recommendation = agent.select_signal_basket(
        candidates=candidates,
        max_picks=2,
        correlation_penalty=0.35,
    )

    assert recommendation.kind is ProblemKind.SIZING_BASKET
    assert recommendation.selected_labels == ["orb", "liquidity"]
    assert recommendation.backend_used == "cache"
    assert recommendation.extra == {"correlation_penalty": 0.35}
    assert "using QUBO" in recommendation.contribution_summary


def test_select_signal_basket_greedy_avoids_adapter_and_rewards_diversity() -> None:
    adapter = _FakeAdapter(_solver_result([1, 1]))
    agent = QuantumOptimizerAgent(adapter=adapter)
    candidates = [
        SignalScore(name="trend", score=0.90, features=[1.0, 0.0]),
        SignalScore(name="trend_clone", score=0.85, features=[1.0, 0.0]),
        SignalScore(name="mean_reversion", score=0.70, features=[0.0, 1.0]),
    ]

    recommendation = agent.select_signal_basket(
        candidates=candidates,
        max_picks=2,
        use_qubo=False,
    )

    assert recommendation.selected_labels == ["trend", "mean_reversion"]
    assert recommendation.backend_used == "tensor_network_greedy"
    assert recommendation.cost_estimate_usd == 0.0
    assert adapter.calls == []


def test_sequence_orders_builds_execution_recommendation() -> None:
    adapter = _FakeAdapter(_solver_result([1, 0, 1], energy=3.25))
    agent = QuantumOptimizerAgent(adapter=adapter)

    recommendation = agent.sequence_orders(
        order_labels=["buy_mnq", "sell_eth", "buy_mbt"],
        impact_estimates_bps=[1.0, 8.0, 2.0],
        adjacency_penalty_bps=0.5,
    )

    assert recommendation.kind is ProblemKind.EXECUTION_SEQUENCING
    assert recommendation.selected_labels == ["buy_mnq", "buy_mbt"]
    assert recommendation.objective == 3.25
    assert recommendation.raw_solution == [1, 0, 1]
    assert "estimated cumulative impact 3.25 bps" in recommendation.contribution_summary
