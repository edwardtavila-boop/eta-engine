"""Quantum optimizer agent (Wave-9 → Wave-10, 2026-04-30).

Plugs into the firm-board as a 6th specialist role + now also feeds
real-time signals into JarvisFull.consult() for intraday optimization.

Six problem types it handles out of the box:

  * PORTFOLIO_ALLOCATION   -- Markowitz mean-variance weights
  * SIZING_BASKET          -- pick K-of-N signals this hour
  * EXECUTION_SEQUENCING   -- order N orders to minimize cumulative slippage
  * RISK_PARITY             -- equalize risk contribution across assets
  * REGIME_AWARE_ALLOCATION -- regime-warped returns/covariance
  * HEDGING_BASKET          -- find optimal hedges for existing positions

It is intentionally OFFLINE-FIRST for the daily rebalance. Real-time
event-driven optimization flows through the new ``fast_optimize()``
method which uses adaptive_solve (auto-selects SA or PT based on size).

Use case (cron job + firm-board hook):

    from eta_engine.brain.jarvis_v3.quantum import QuantumOptimizerAgent
    from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
        CloudConfig, QuantumCloudAdapter,
    )

    agent = QuantumOptimizerAgent(
        adapter=QuantumCloudAdapter(CloudConfig(enable_cloud=False)),
    )

    rec = agent.allocate_portfolio(
        symbols=["MNQ", "BTC", "ETH", "MBT"],
        expected_returns=[1.2, 0.9, 0.7, 0.5],
        covariance=cov_matrix,
        target_n_positions=2,
    )
    print(rec.selected_symbols, rec.objective)

Output is consumed by the firm-board:
  - Researcher reads ``rec.contribution_summary`` for narrative
  - Risk Committee checks ``rec.cardinality`` against fleet caps
  - Executor uses ``rec.execution_order`` if present
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from eta_engine.brain.jarvis_v3.quantum.cloud_adapter import (
    QuantumCloudAdapter,
)
from eta_engine.brain.jarvis_v3.quantum.qubo_solver import (
    portfolio_allocation_qubo,
    sizing_basket_qubo,
)
from eta_engine.brain.jarvis_v3.quantum.tensor_network import (
    SignalScore,
    select_top_signal_combination,
    signal_correlation_matrix,
)

logger = logging.getLogger(__name__)


class ProblemKind(StrEnum):
    PORTFOLIO_ALLOCATION = "PORTFOLIO_ALLOCATION"
    SIZING_BASKET = "SIZING_BASKET"
    EXECUTION_SEQUENCING = "EXECUTION_SEQUENCING"
    RISK_PARITY = "RISK_PARITY"
    REGIME_AWARE_ALLOCATION = "REGIME_AWARE_ALLOCATION"
    HEDGING_BASKET = "HEDGING_BASKET"


@dataclass
class Recommendation:
    """Structured output of one quantum-optimizer call."""

    ts: str
    kind: ProblemKind
    selected_labels: list[str]
    objective: float
    backend_used: str
    n_vars: int
    runtime_ms: float
    cost_estimate_usd: float
    used_cache: bool
    fell_back_to_classical: bool
    contribution_summary: str  # operator-readable narrative
    raw_solution: list[int] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


# ─── Agent ─────────────────────────────────────────────────────────


class QuantumOptimizerAgent:
    """Firm-board pluggable agent.

    Stateless except for the underlying QuantumCloudAdapter, which
    does its own caching + audit logging. Methods are intentionally
    coarse-grained (one method per problem kind) so the agent stays
    grokkable; underlying QUBO encoders are exposed in qubo_solver.py
    if a caller wants finer control.

    Wave-18 cost discipline (2026-04-30):
      * Quantum invocation gated by ``should_invoke()`` — only runs when
        edge benefit justifies cost
      * Classical SA for n_vars <= 8; adaptive (SA/PT) for 9-16; full PT
        for > 16
      * Cloud quantum NEVER invoked from fast_optimize() (hot path)
      * Daily budget tracked per-agent; auto-throttle at ceiling
    """

    # Cost-gating thresholds
    MIN_ASSETS_FOR_QUANTUM = 3
    CLASSICAL_THRESHOLD = 8
    ADAPTIVE_THRESHOLD = 16

    def __init__(
        self,
        *,
        adapter: QuantumCloudAdapter | None = None,
        n_iterations: int = 5_000,
        cost_budget_daily_usd: float = 2.00,
    ) -> None:
        self.adapter = adapter or QuantumCloudAdapter()
        self.n_iterations = n_iterations
        self.cost_budget_daily = cost_budget_daily_usd
        self._spent_today = 0.0
        self._spent_date = ""

    @staticmethod
    def should_invoke(
        *,
        n_symbols: int,
        regime_changed_since_last: bool = True,
        volatility_changed_pct: float = 0.0,
        last_invoked_seconds_ago: float | None = None,
    ) -> tuple[bool, str]:
        """Gatekeeper: should we spend compute on quantum optimization?

        Returns (should_invoke, reason).

        Rules (first NO wins):
          1. n_symbols < MIN_ASSETS_FOR_QUANTUM → NO (not enough assets)
          2. No regime change + no vol spike → NO (nothing new to optimize)
          3. Invoked < 60 seconds ago → NO (rate limit)
          4. Otherwise → YES
        """
        if n_symbols < QuantumOptimizerAgent.MIN_ASSETS_FOR_QUANTUM:
            return False, (f"portfolio_size={n_symbols} < min={QuantumOptimizerAgent.MIN_ASSETS_FOR_QUANTUM}")
        if not regime_changed_since_last and volatility_changed_pct < 0.15:
            return False, "no regime change and vol stable — rebalance not needed"
        if last_invoked_seconds_ago is not None and last_invoked_seconds_ago < 60:
            return False, f"rate limit: last invoked {last_invoked_seconds_ago:.0f}s ago"
        return True, "regime or vol shift — quantum edge justified"

    def _check_budget(self) -> bool:
        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%Y%m%d")
        if today != self._spent_date:
            self._spent_today = 0.0
            self._spent_date = today
        return self._spent_today < self.cost_budget_daily

    def _spend(self, amount: float) -> None:
        self._spent_today += amount

    # ── Portfolio allocation ────────────────────────────────

    def allocate_portfolio(
        self,
        *,
        symbols: list[str],
        expected_returns: list[float],
        covariance: list[list[float]],
        risk_aversion: float = 1.0,
        target_n_positions: int | None = None,
    ) -> Recommendation:
        """Run mean-variance allocation as a QUBO."""
        problem = portfolio_allocation_qubo(
            expected_returns=expected_returns,
            covariance=covariance,
            risk_aversion=risk_aversion,
            cardinality_min=target_n_positions,
            cardinality_max=target_n_positions,
            asset_labels=symbols,
        )
        result, record = self.adapter.solve(
            problem,
            n_iterations=self.n_iterations,
        )
        selected = result.selected_labels()
        contrib = (
            f"Quantum-optimizer (backend={record.backend}) selected "
            f"{len(selected)}/{len(symbols)} symbols "
            f"({', '.join(selected) if selected else 'none'}) "
            f"with portfolio objective {result.energy:+.4f}; "
            f"target cardinality "
            f"{target_n_positions if target_n_positions else 'unconstrained'}."
        )
        return Recommendation(
            ts=datetime.now(UTC).isoformat(),
            kind=ProblemKind.PORTFOLIO_ALLOCATION,
            selected_labels=selected,
            objective=result.energy,
            backend_used=record.backend,
            n_vars=record.n_vars,
            runtime_ms=record.runtime_ms,
            cost_estimate_usd=record.cost_estimate_usd,
            used_cache=record.used_cache,
            fell_back_to_classical=record.fell_back_to_classical,
            contribution_summary=contrib,
            raw_solution=list(result.x),
        )

    # ── K-of-N signal basket ────────────────────────────────

    def select_signal_basket(
        self,
        *,
        candidates: list[SignalScore],
        max_picks: int,
        correlation_penalty: float = 0.5,
        use_qubo: bool = True,
    ) -> Recommendation:
        """Pick ``max_picks`` signals out of ``candidates``.

        ``use_qubo=True`` uses the QUBO + simulated-annealing global
        optimizer; ``use_qubo=False`` uses the lighter tensor-network
        diversity-aware greedy. The greedy is faster but not provably
        optimal; the QUBO is what you want for nightly rebalancing.
        """
        if use_qubo:
            corr = signal_correlation_matrix(candidates)
            problem = sizing_basket_qubo(
                expected_r=[c.score for c in candidates],
                pairwise_correlation=corr,
                correlation_penalty=correlation_penalty,
                max_picks=max_picks,
                signal_labels=[c.name for c in candidates],
            )
            result, record = self.adapter.solve(
                problem,
                n_iterations=self.n_iterations,
            )
            selected = result.selected_labels()
            contrib = (
                f"Quantum-optimizer picked "
                f"{len(selected)} signals out of {len(candidates)} "
                f"using QUBO + {record.backend}: "
                f"{', '.join(selected) if selected else 'none'}. "
                f"Objective {result.energy:+.4f}."
            )
            return Recommendation(
                ts=datetime.now(UTC).isoformat(),
                kind=ProblemKind.SIZING_BASKET,
                selected_labels=selected,
                objective=result.energy,
                backend_used=record.backend,
                n_vars=record.n_vars,
                runtime_ms=record.runtime_ms,
                cost_estimate_usd=record.cost_estimate_usd,
                used_cache=record.used_cache,
                fell_back_to_classical=record.fell_back_to_classical,
                contribution_summary=contrib,
                raw_solution=list(result.x),
                extra={"correlation_penalty": correlation_penalty},
            )
        # Greedy diversity-aware path
        combo = select_top_signal_combination(candidates, k=max_picks)
        contrib = (
            f"Tensor-network selector picked {len(combo.selected)} "
            f"signals: {', '.join(s.name for s in combo.selected)}; "
            f"raw_score={combo.total_raw_score:.3f}, "
            f"diversity={combo.total_diversity_score:.3f}."
        )
        return Recommendation(
            ts=datetime.now(UTC).isoformat(),
            kind=ProblemKind.SIZING_BASKET,
            selected_labels=[s.name for s in combo.selected],
            objective=combo.objective,
            backend_used="tensor_network_greedy",
            n_vars=len(candidates),
            runtime_ms=0.0,
            cost_estimate_usd=0.0,
            used_cache=False,
            fell_back_to_classical=False,
            contribution_summary=contrib,
        )

    # ── Order execution sequencing ──────────────────────────

    def sequence_orders(
        self,
        *,
        order_labels: list[str],
        impact_estimates_bps: list[float],
        adjacency_penalty_bps: float = 1.0,
    ) -> Recommendation:
        """Choose which orders (subset) to send THIS slice to minimize
        cumulative slippage, leaving high-impact orders to TWAP across
        later slices.

        Modeled as: minimize sum_i impact[i] * x_i + penalty for picking
        too many at once.
        """
        n = len(order_labels)
        q: dict[int, dict[int, float]] = {}
        for i in range(n):
            q.setdefault(i, {})[i] = impact_estimates_bps[i]
            for j in range(n):
                if i == j:
                    continue
                q[i][j] = adjacency_penalty_bps
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem

        problem = QuboProblem(n_vars=n, Q=q, labels=order_labels)
        result, record = self.adapter.solve(
            problem,
            n_iterations=self.n_iterations,
        )
        selected = result.selected_labels()
        contrib = (
            f"Execution sequencer picked {len(selected)}/{n} orders "
            f"to send this slice ({', '.join(selected)}); "
            f"estimated cumulative impact {result.energy:.2f} bps."
        )
        return Recommendation(
            ts=datetime.now(UTC).isoformat(),
            kind=ProblemKind.EXECUTION_SEQUENCING,
            selected_labels=selected,
            objective=result.energy,
            backend_used=record.backend,
            n_vars=record.n_vars,
            runtime_ms=record.runtime_ms,
            cost_estimate_usd=record.cost_estimate_usd,
            used_cache=record.used_cache,
            fell_back_to_classical=record.fell_back_to_classical,
            contribution_summary=contrib,
            raw_solution=list(result.x),
        )

    # ── Risk-parity allocation ──────────────────────────────

    def allocate_risk_parity(
        self,
        *,
        symbols: list[str],
        expected_returns: list[float],
        covariance: list[list[float]],
        risk_aversion: float = 1.0,
        max_assets: int | None = None,
    ) -> Recommendation:
        """Equalize risk contribution across selected assets.

        Useful for multi-asset portfolios where concentration risk
        must be kept in check (e.g. BTC + ETH + SOL + MNQ basket).
        """
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import (
            risk_parity_qubo,
        )

        problem = risk_parity_qubo(
            expected_returns=expected_returns,
            covariance=covariance,
            risk_aversion=risk_aversion,
            max_assets=max_assets,
            asset_labels=symbols,
        )
        result, record = self.adapter.solve(
            problem,
            n_iterations=self.n_iterations,
        )
        selected = result.selected_labels()
        contrib = (
            f"Risk-parity optimizer (backend={record.backend}) selected "
            f"{len(selected)}/{len(symbols)} symbols "
            f"({', '.join(selected) if selected else 'none'}) "
            f"with risk-parity energy {result.energy:+.4f}"
        )
        return Recommendation(
            ts=datetime.now(UTC).isoformat(),
            kind=ProblemKind.RISK_PARITY,
            selected_labels=selected,
            objective=result.energy,
            backend_used=record.backend,
            n_vars=record.n_vars,
            runtime_ms=record.runtime_ms,
            cost_estimate_usd=record.cost_estimate_usd,
            used_cache=record.used_cache,
            fell_back_to_classical=record.fell_back_to_classical,
            contribution_summary=contrib,
            raw_solution=list(result.x),
        )

    # ── Regime-aware allocation ─────────────────────────────

    def allocate_regime_aware(
        self,
        *,
        symbols: list[str],
        expected_returns: list[float],
        covariance: list[list[float]],
        modifiers: list,  # RegimeModifier list
        risk_aversion: float = 1.0,
        cardinality_max: int | None = None,
    ) -> Recommendation:
        """Allocate across assets with regime-warped expectations.

        Each asset gets a return_multiplier and risk_multiplier based
        on how the current regime treats it. Assets with tailwinds
        get boosted; headwind assets get penalized.
        """
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import (
            regime_aware_qubo,
        )

        problem = regime_aware_qubo(
            expected_returns=expected_returns,
            covariance=covariance,
            modifiers=modifiers,
            risk_aversion=risk_aversion,
            cardinality_max=cardinality_max,
            asset_labels=symbols,
        )
        result, record = self.adapter.solve(
            problem,
            n_iterations=self.n_iterations,
        )
        selected = result.selected_labels()
        contrib = (
            f"Regime-aware optimizer (backend={record.backend}) with "
            f"{len(modifiers)} regime modifier(s) selected "
            f"{len(selected)}/{len(symbols)} symbols "
            f"({', '.join(selected) if selected else 'none'}); "
            f"energy {result.energy:+.4f}"
        )
        return Recommendation(
            ts=datetime.now(UTC).isoformat(),
            kind=ProblemKind.REGIME_AWARE_ALLOCATION,
            selected_labels=selected,
            objective=result.energy,
            backend_used=record.backend,
            n_vars=record.n_vars,
            runtime_ms=record.runtime_ms,
            cost_estimate_usd=record.cost_estimate_usd,
            used_cache=record.used_cache,
            fell_back_to_classical=record.fell_back_to_classical,
            contribution_summary=contrib,
            raw_solution=list(result.x),
            extra={"n_modifiers": len(modifiers)},
        )

    # ── Hedging basket optimization ─────────────────────────

    def select_hedges(
        self,
        *,
        positions: list[float],
        candidates: list[float],
        pairwise_correlation: list[list[float]],
        target_net_beta: float = 0.0,
        max_hedges: int | None = None,
        position_labels: list[str] | None = None,
        hedge_labels: list[str] | None = None,
    ) -> Recommendation:
        """Select optimal hedging instruments for existing positions.

        Given current positions and candidate hedge instruments,
        finds the subset that brings the portfolio beta closest to
        the target (e.g., 0 for delta-neutral).
        """
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import (
            hedging_basket_qubo,
        )

        problem = hedging_basket_qubo(
            positions=positions,
            candidates=candidates,
            pairwise_correlation=pairwise_correlation,
            target_net_beta=target_net_beta,
            max_hedges=max_hedges,
            position_labels=position_labels,
            hedge_labels=hedge_labels,
        )
        result, record = self.adapter.solve(
            problem,
            n_iterations=self.n_iterations,
        )
        selected = result.selected_labels()
        contrib = (
            f"Hedging optimizer (backend={record.backend}) selected "
            f"{len(selected)} hedges out of {len(candidates)} candidates "
            f"({', '.join(selected) if selected else 'none'}); "
            f"target beta {target_net_beta:+.2f}, energy {result.energy:+.4f}"
        )
        return Recommendation(
            ts=datetime.now(UTC).isoformat(),
            kind=ProblemKind.HEDGING_BASKET,
            selected_labels=selected,
            objective=result.energy,
            backend_used=record.backend,
            n_vars=record.n_vars,
            runtime_ms=record.runtime_ms,
            cost_estimate_usd=record.cost_estimate_usd,
            used_cache=record.used_cache,
            fell_back_to_classical=record.fell_back_to_classical,
            contribution_summary=contrib,
            raw_solution=list(result.x),
        )

    # ── Fast real-time optimization (event-driven) ──────────

    def fast_optimize(
        self,
        *,
        problem: ProblemKind,
        symbols: list[str] | None = None,
        expected_returns: list[float] | None = None,
        covariance: list[list[float]] | None = None,
        risk_aversion: float = 1.0,
        max_picks: int | None = None,
        **kwargs: object,
    ) -> Recommendation:
        """Real-time event-driven optimization — uses adaptive solver.

        Designed for intraday triggers (regime change, volatility spike,
        news event). Uses adaptive_solve (auto-selects SA or parallel
        tempering based on problem size). NEVER uses cloud quantum —
        pure classical path for latency guarantees.
        """
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import (
            adaptive_solve,
        )

        n_vars = len(symbols) if symbols else (len(expected_returns) if expected_returns else 0)
        if n_vars == 0:
            return Recommendation(
                ts=datetime.now(UTC).isoformat(),
                kind=problem,
                selected_labels=[],
                objective=0.0,
                backend_used="none",
                n_vars=0,
                runtime_ms=0.0,
                cost_estimate_usd=0.0,
                used_cache=False,
                fell_back_to_classical=False,
                contribution_summary="No variables to optimize.",
            )

        if problem == ProblemKind.PORTFOLIO_ALLOCATION:
            q_problem = portfolio_allocation_qubo(
                expected_returns=expected_returns or [0.0] * n_vars,
                covariance=covariance or [[1.0 if i == j else 0.0 for j in range(n_vars)] for i in range(n_vars)],
                risk_aversion=risk_aversion,
                cardinality_max=max_picks,
                asset_labels=symbols,
            )
        else:
            q_problem = QuboProblem(n_vars=n_vars, Q={}, labels=symbols or [])
            for i in range(n_vars):
                q_problem.Q.setdefault(i, {})[i] = -(expected_returns[i] if expected_returns else 0.0)

        t0 = __import__("time").perf_counter()
        result = adaptive_solve(q_problem, n_iterations=max(1000, self.n_iterations // 2))
        elapsed_ms = (__import__("time").perf_counter() - t0) * 1000.0
        selected = result.selected_labels()

        contrib = (
            f"Fast-optimize ({problem.value}, adaptive PT/SA) selected "
            f"{len(selected)}/{n_vars}: "
            f"{', '.join(selected) if selected else 'none'}; "
            f"energy {result.energy:+.4f}, {result.n_iterations} iters, "
            f"{elapsed_ms:.1f}ms"
        )
        return Recommendation(
            ts=datetime.now(UTC).isoformat(),
            kind=problem,
            selected_labels=selected,
            objective=result.energy,
            backend_used="adaptive_classical",
            n_vars=n_vars,
            runtime_ms=round(elapsed_ms, 1),
            cost_estimate_usd=0.0,
            used_cache=False,
            fell_back_to_classical=False,
            contribution_summary=contrib,
            raw_solution=list(result.x),
        )
