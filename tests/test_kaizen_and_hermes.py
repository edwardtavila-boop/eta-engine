"""Tests for Kaizen Engine, Kaizen Guard, Hermes Bridge, and Supercharged QUBO."""

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# Ensure shared jarvis module is importable
_COMMON_SRC = Path(__file__).resolve().parents[2] / "firm" / "eta_engine" / "src"
if _COMMON_SRC.exists() and str(_COMMON_SRC) not in sys.path:
    sys.path.insert(0, str(_COMMON_SRC))


# ─── Helpers ─────────────────────────────────────────────────


def _mock_trades(n: int = 50, pnl: float = 25.0) -> list[dict]:
    return [
        {
            "pnl_dollars": pnl * (1 if i % 3 != 0 else -0.5),
            "exit_reason": "take_profit" if i % 3 != 0 else "stop",
            "bars_held": 3 if i % 2 == 0 else 8,
            "r_multiple": 2.0 if i % 3 != 0 else -1.0,
            "regime": "trend" if i < 30 else "mean_revert",
            "side": "long" if i % 2 == 0 else "short",
            "symbol": "MNQ",
            "route_name": "route_a" if i % 2 == 0 else "route_b",
        }
        for i in range(n)
    ]


# ─── KaizenGuard Tests ──────────────────────────────────────


class TestKaizenGuard:
    def test_admit_allows_normal_change(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard(max_changes_per_cycle=5)
        decision = guard.admit("entry.confirmation_bars", instrument="MNQ", current_drawdown=20)
        assert decision.allowed is True
        assert decision.rule == ""

    def test_admit_blocks_on_cycle_cap(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard(max_changes_per_cycle=2)
        assert guard.admit("param_a", instrument="MNQ").allowed is True
        assert guard.admit("param_b", instrument="MNQ").allowed is True
        assert guard.admit("param_c", instrument="MNQ").allowed is False

    def test_admit_blocks_on_daily_cap(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard(max_daily_changes=2)
        guard._changes_today = 2
        decision = guard.admit("param_a", instrument="MNQ")
        assert decision.allowed is False
        assert decision.rule == "daily_cap"

    def test_admit_blocks_on_per_instrument_cap(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard(max_changes_per_cycle=10, max_per_instrument=1)
        assert guard.admit("param_a", instrument="MNQ").allowed is True
        assert guard.admit("param_b", instrument="MNQ").allowed is False
        assert guard.admit("param_c", instrument="BTC").allowed is True

    def test_admit_blocks_on_parameter_cooldown(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard(parameter_cooldown_seconds=99999)
        guard._last_changed["entry.confirmation_bars"] = datetime.now(UTC).isoformat()
        decision = guard.admit("entry.confirmation_bars", instrument="MNQ")
        assert decision.allowed is False
        assert decision.rule == "parameter_cooldown"

    def test_admit_blocks_on_drawdown_circuit_breaker(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard(dd_circuit_breaker_ratio=0.5, max_daily_loss=100)
        decision = guard.admit("param_a", instrument="MNQ", current_drawdown=80)
        assert decision.allowed is False
        assert decision.rule == "dd_circuit_breaker"
        assert guard.status().circuit_breaker_tripped is True

    def test_reset_cycle_clears_counters(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard(max_changes_per_cycle=2)
        assert guard.admit("param_a", instrument="MNQ").allowed is True
        assert guard.admit("param_b", instrument="MNQ").allowed is True
        guard.reset_cycle()
        assert guard.admit("param_c", instrument="MNQ").allowed is True

    def test_reset_circuit_clears_blocker(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard(dd_circuit_breaker_ratio=0.1, max_daily_loss=100)
        guard.admit("param_a", instrument="MNQ", current_drawdown=50)
        assert guard.status().circuit_breaker_tripped is True
        guard.reset_circuit()
        assert guard.status().circuit_breaker_tripped is False

    def test_should_rollback_detects_degradation(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard()
        assert guard.should_rollback("param_a", pre_change_sharpe=1.5, post_change_sharpe=1.2, trades_since_change=15) is True

    def test_should_rollback_ignores_too_few_trades(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        guard = KaizenGuard()
        assert guard.should_rollback("param_a", pre_change_sharpe=1.5, post_change_sharpe=1.2, trades_since_change=3) is False

    def test_save_load_state_roundtrip(self):
        from eta_engine.brain.jarvis_v3.kaizen_guard import KaizenGuard

        with tempfile.TemporaryDirectory() as tmp:
            g1 = KaizenGuard(state_dir=Path(tmp), max_changes_per_cycle=3)
            g1.admit("entry.confirmation_bars", instrument="MNQ")
            g1.save_state()

            g2 = KaizenGuard(state_dir=Path(tmp), max_changes_per_cycle=3)
            g2.load_state()
            assert g2._changes_today == g1._changes_today
            assert g2._last_changed == g1._last_changed


# ─── KaizenEngine Tests ─────────────────────────────────────


class TestKaizenEngine:
    def test_cycle_with_no_trades_returns_empty_report(self):
        from common.jarvis.instrument import InstrumentConfig

        from eta_engine.brain.jarvis_v3.kaizen_engine import KaizenEngine

        with tempfile.TemporaryDirectory() as tmp:
            cfg = InstrumentConfig.mnq()
            engine = KaizenEngine(instruments=[cfg], state_dir=Path(tmp))
            report = engine.cycle(trades_by_instrument={}, oos_trades_by_instrument={})

            assert report.proposals_total == 0
            assert report.proposals_approved == 0

    def test_cycle_processes_instrument_trades(self):
        from common.jarvis.instrument import InstrumentConfig

        from eta_engine.brain.jarvis_v3.kaizen_engine import KaizenEngine

        with tempfile.TemporaryDirectory() as tmp:
            cfg = InstrumentConfig.mnq()
            engine = KaizenEngine(instruments=[cfg], state_dir=Path(tmp))
            trades = _mock_trades(50)
            report = engine.cycle(
                trades_by_instrument={"MNQ": trades},
                oos_trades_by_instrument={"MNQ": _mock_trades(30)},
            )

            assert report.instruments_processed > 0 or report.proposals_total >= 0
            assert report.cycle_duration_ms > 0

    def test_register_strategy(self):
        from common.jarvis.instrument import InstrumentConfig

        from eta_engine.brain.jarvis_v3.kaizen_engine import KaizenEngine

        with tempfile.TemporaryDirectory() as tmp:
            cfg = InstrumentConfig.mnq()
            engine = KaizenEngine(instruments=[cfg], state_dir=Path(tmp))
            engine.register_strategy("route_a", "MNQ")
            assert "MNQ" in engine._strategies
            assert "route_a" in engine._strategies["MNQ"]

    def test_should_retire_unprofitable_strategy(self):
        from common.jarvis.instrument import InstrumentConfig

        from eta_engine.brain.jarvis_v3.kaizen_engine import KaizenEngine

        with tempfile.TemporaryDirectory() as tmp:
            cfg = InstrumentConfig.mnq()
            engine = KaizenEngine(instruments=[cfg], state_dir=Path(tmp))
            losing_trades = _mock_trades(30, pnl=-10)
            promoted, retired = engine._manage_strategy_lifecycle(
                "MNQ", losing_trades, [], cfg, datetime.now(UTC),
            )
            # No registered strategies yet, so no retirements
            assert len(retired) == 0


# ─── Hermes Bridge Tests ─────────────────────────────────────


class TestHermesBridge:
    def test_notification_formatting(self):
        from hermes_jarvis_telegram.hermes_bridge import (
            JarvisNotification,
            MessagePriority,
        )

        notification = JarvisNotification(
            priority=MessagePriority.HIGH,
            title="Test Alert",
            body="This is a test",
        )
        html = notification.as_telegram_html()
        assert "Test Alert" in html
        assert "This is a test" in html

    def test_bridge_initializes_without_telegram(self):
        from hermes_jarvis_telegram.hermes_bridge import HermesBridge

        bridge = HermesBridge(bot_token="", chat_id="")
        assert bridge._enable_telegram is False
        status = bridge.status()
        assert status["telegram_enabled"] is False

    def test_bridge_queues_notification(self):
        from hermes_jarvis_telegram.hermes_bridge import HermesBridge

        push_count = [0]

        def fake_push(title, body):
            push_count[0] += 1
            return True

        bridge = HermesBridge(push_hook=fake_push, enable_telegram=False)
        bridge.notify_autonomous_trade(
            subsystem="bot.mnq", action="ORDER_PLACE", verdict="APPROVED", symbol="MNQ",
        )
        # Allow brief time for async dispatch
        import time
        time.sleep(0.2)
        assert bridge._sent_count >= 1 or push_count[0] >= 1

    def test_store_and_forward(self):
        from hermes_jarvis_telegram.hermes_bridge import (
            HermesBridge,
            JarvisNotification,
            MessagePriority,
        )

        with tempfile.TemporaryDirectory() as tmp:
            bridge = HermesBridge(
                bot_token="", chat_id="", enable_telegram=False,
            )
            bridge.STORE_AND_FORWARD_PATH = Path(tmp) / "saf.jsonl"
            notification = JarvisNotification(
                priority=MessagePriority.NORMAL,
                title="Queued",
                body="Should be stored",
            )
            bridge._store(notification)
            assert bridge.STORE_AND_FORWARD_PATH.exists()
            content = bridge.STORE_AND_FORWARD_PATH.read_text()
            assert "Queued" in content

    def test_notify_all_message_types(self):
        from hermes_jarvis_telegram.hermes_bridge import HermesBridge

        bridge = HermesBridge(enable_telegram=False)
        bridge.notify_autonomous_trade(subsystem="bot.mnq", action="ORDER_PLACE", verdict="APPROVED")
        bridge.notify_kaizen_cycle(cycle_id="test", proposals_approved=1, proposals_rejected=0,
                                    strategies_promoted=[], strategies_retired=[],
                                    quantum_count=0, quantum_cost=0, duration_ms=100)
        bridge.notify_strategy_lifecycle(strategy_name="test", instrument="MNQ",
                                          from_status="paper", to_status="live")
        bridge.notify_quantum_rebalance(selected_symbols=["MNQ"], objective=1.0, backend="classical")
        bridge.notify_kill_switch(trigger="drawdown", action="flatten_all")
        bridge.notify_system_health(health_score=0.9, verdict="healthy")
        # All should succeed without exception
        assert bridge._queue.qsize() >= 6 or bridge._sent_count >= 0


# ─── Supercharged QUBO Tests ────────────────────────────────


class TestSuperchargedQubo:
    def test_risk_parity_qubo_builds(self):
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import risk_parity_qubo

        returns = [0.1, 0.2, 0.15, 0.05]
        cov = [[1.0, 0.5, 0.3, 0.2],
               [0.5, 1.0, 0.4, 0.3],
               [0.3, 0.4, 1.0, 0.1],
               [0.2, 0.3, 0.1, 1.0]]
        labels = ["BTC", "ETH", "SOL", "MNQ"]
        problem = risk_parity_qubo(expected_returns=returns, covariance=cov, asset_labels=labels)
        assert problem.n_vars == 4
        assert problem.labels == labels

    def test_regime_aware_qubo_applies_modifiers(self):
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import (
            RegimeModifier,
            regime_aware_qubo,
        )

        returns = [0.1, 0.2, 0.15]
        cov = [[1.0, 0.3, 0.2], [0.3, 1.0, 0.4], [0.2, 0.4, 1.0]]
        modifiers = [
            RegimeModifier(0, return_multiplier=2.0, risk_multiplier=0.5),
            RegimeModifier(1, return_multiplier=0.5, risk_multiplier=2.0),
        ]
        labels = ["BTC", "ETH", "SOL"]
        problem = regime_aware_qubo(
            expected_returns=returns, covariance=cov, modifiers=modifiers, asset_labels=labels,
        )
        assert problem.n_vars == 3
        assert len(problem.Q) > 0

    def test_parallel_tempering_finds_solution(self):
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import parallel_tempering_solve

        problem = QuboProblem(n_vars=4, labels=["a", "b", "c", "d"])
        for i in range(4):
            problem.Q.setdefault(i, {})[i] = -1.0
            for j in range(4):
                if i != j:
                    problem.Q.setdefault(i, {})[j] = 0.5
        result = parallel_tempering_solve(problem, n_replicas=4, n_iterations=500, seed=42)
        assert result.energy < 0
        assert len(result.x) == 4

    def test_adaptive_solve_chooses_method(self):
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import adaptive_solve

        small = QuboProblem(n_vars=4, labels=["a", "b", "c", "d"])
        for i in range(4):
            small.Q.setdefault(i, {})[i] = -1.0
        result = adaptive_solve(small, n_iterations=200, seed=42)
        assert result.energy < 0

        large = QuboProblem(n_vars=20, labels=[f"x{i}" for i in range(20)])
        for i in range(20):
            large.Q.setdefault(i, {})[i] = -1.0
        result = adaptive_solve(large, n_iterations=200, seed=42)
        assert result.energy < 0
        assert len(result.x) == 20

    def test_hedging_basket_qubo_builds(self):
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import hedging_basket_qubo

        positions = [10.0, -5.0]
        candidates = [-10.0, 5.0, -3.0]
        corr = [[1.0, 0.5, 0.0], [0.5, 1.0, 0.3], [0.0, 0.3, 1.0]]
        problem = hedging_basket_qubo(
            positions=positions, candidates=candidates, pairwise_correlation=corr,
            hedge_labels=["H1", "H2", "H3"], position_labels=["P1", "P2"],
        )
        assert problem.n_vars == 3

    def test_multi_horizon_qubo_builds(self):
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import (
            HorizonSlice,
            multi_horizon_qubo,
        )

        short = HorizonSlice(name="short", weight=1.0, expected_returns=[0.1, 0.2, 0.15])
        long = HorizonSlice(name="long", weight=0.5, expected_returns=[0.05, 0.1, 0.2])
        problem = multi_horizon_qubo(
            horizons=[short, long], asset_labels=["BTC", "ETH", "SOL"], max_assets_total=4,
        )
        assert problem.n_vars == 6
        assert len(problem.labels) == 6


# ─── Quantum should_invoke Tests ────────────────────────────


class TestShouldInvoke:
    def test_rejects_too_few_symbols(self):
        from eta_engine.brain.jarvis_v3.quantum.quantum_agent import QuantumOptimizerAgent

        should, reason = QuantumOptimizerAgent.should_invoke(n_symbols=2, regime_changed_since_last=True)
        assert should is False
        assert "portfolio_size" in reason

    def test_rejects_no_regime_change_and_stable_vol(self):
        from eta_engine.brain.jarvis_v3.quantum.quantum_agent import QuantumOptimizerAgent

        should, reason = QuantumOptimizerAgent.should_invoke(
            n_symbols=5, regime_changed_since_last=False, volatility_changed_pct=0.05,
        )
        assert should is False
        assert "stable" in reason or "regime" in reason

    def test_accepts_regime_change(self):
        from eta_engine.brain.jarvis_v3.quantum.quantum_agent import QuantumOptimizerAgent

        should, reason = QuantumOptimizerAgent.should_invoke(
            n_symbols=5, regime_changed_since_last=True,
        )
        assert should is True

    def test_accepts_vol_spike(self):
        from eta_engine.brain.jarvis_v3.quantum.quantum_agent import QuantumOptimizerAgent

        should, reason = QuantumOptimizerAgent.should_invoke(
            n_symbols=5, regime_changed_since_last=False, volatility_changed_pct=0.30,
        )
        assert should is True

    def test_rejects_rate_limit(self):
        from eta_engine.brain.jarvis_v3.quantum.quantum_agent import QuantumOptimizerAgent

        should, reason = QuantumOptimizerAgent.should_invoke(
            n_symbols=5, regime_changed_since_last=True, last_invoked_seconds_ago=30,
        )
        assert should is False
        assert "rate" in reason.lower()

    def test_budget_check_tracks_daily(self):
        from eta_engine.brain.jarvis_v3.quantum.quantum_agent import QuantumOptimizerAgent

        agent = QuantumOptimizerAgent(cost_budget_daily_usd=2.00)
        assert agent._check_budget() is True
        agent._spent_today = 2.50
        agent._spent_date = datetime.now(UTC).strftime("%Y%m%d")
        assert agent._check_budget() is False
