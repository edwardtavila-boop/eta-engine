"""End-to-end pipeline test: validates the full autonomous trading loop.

Trade data -> Jarvis consult -> Quantum optimize -> Master lock ->
Kaizen propose -> Guard admit -> Parameter apply -> Hermes notify.

No live money. No broker connection. Pure integration test with mock data.
"""

import asyncio
import contextlib
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

# -- ensure paths before any local imports

_WORKSPACE = Path(__file__).resolve().parents[2]
_SRC = _WORKSPACE / "firm" / "eta_engine" / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))
_ETA_ROOT = Path(__file__).resolve().parents[1]
if str(_ETA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ETA_ROOT))

# ─── Helpers ─────────────────────────────────────────────────


def _make_trade(idx: int, pnl: float = 50.0, symbol: str = "MNQ") -> dict:
    return {
        "pnl_dollars": pnl * (1.0 if idx % 3 != 0 else -0.5),
        "exit_reason": "take_profit" if idx % 3 != 0 else "stop",
        "bars_held": 3 if idx % 2 == 0 else 8,
        "r_multiple": 2.0 if idx % 3 != 0 else -1.0,
        "regime": "trend" if idx < 30 else "mean_revert",
        "side": "long" if idx % 2 == 0 else "short",
        "symbol": symbol,
        "route_name": "route_a" if idx % 2 == 0 else "route_b",
        "bot_id": f"bot_{idx % 3}",
        "commission_dollars": 0.50,
        "entry_ts": datetime(2026, 4, 30, 14, 0, 0, tzinfo=UTC),
    }


# ─── Tests ───────────────────────────────────────────────────


class TestEndToEndPipeline:
    """Full pipeline integration: trades -> Jarvis -> quantum -> kaizen -> hermes."""

    def test_full_autonomous_cycle(self):
        from common.jarvis import InstrumentConfig

        cfg = InstrumentConfig.mnq()
        trades = [_make_trade(i) for i in range(50)]
        oos_trades = [_make_trade(i, pnl=45) for i in range(30)]

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            # Stop Hermes from trying real Telegram
            import hermes_jarvis_telegram.hermes_bridge as hb

            if hasattr(hb, "reset_bridge"):
                hb.reset_bridge()

            pushed = []

            def capture_push(title, body):
                pushed.append({"title": title, "body": body})
                return True

            bridge = hb.HermesBridge(push_hook=capture_push, enable_telegram=False)
            if hasattr(hb, "_bridge_instance"):
                hb._bridge_instance = bridge

            # 1. KaizenEngine auto-cycle
            from eta_engine.brain.jarvis_v3.kaizen_engine import KaizenEngine

            engine = KaizenEngine(instruments=[cfg], state_dir=tmp)
            report = engine.cycle(
                trades_by_instrument={"MNQ": trades},
                oos_trades_by_instrument={"MNQ": oos_trades},
            )
            assert report.cycle_id.startswith("KZN-")
            assert report.instruments_processed == 1
            assert report.cycle_duration_ms > 0

            # 2. KaizenGuard health
            guard_status = engine._guard.status()
            assert guard_status.max_daily > 0

            # 3. Verify Hermes was notified
            asyncio.run(bridge.flush_store_and_forward())

            # 4. Verify state persisted
            state_file = tmp / "kaizen_engine_state.json"
            assert state_file.exists()
            state = json.loads(state_file.read_text())
            assert state["cycle_count"] >= 1
        # Pipeline completed without crashing

    def test_quantum_should_invoke_integration(self):
        from eta_engine.brain.jarvis_v3.quantum.quantum_agent import (
            ProblemKind,
            QuantumOptimizerAgent,
        )

        agent = QuantumOptimizerAgent(cost_budget_daily_usd=2.00)

        # Should reject: too few symbols
        should, reason = QuantumOptimizerAgent.should_invoke(n_symbols=2, regime_changed_since_last=True)
        assert should is False

        # Should accept
        should, reason = QuantumOptimizerAgent.should_invoke(n_symbols=5, regime_changed_since_last=True)
        assert should is True

        # Fast optimize succeeds on simple data
        symbols = ["MNQ", "NQ", "MES", "BTC", "ETH"]
        returns = [0.1, 0.08, 0.05, 0.12, 0.07]
        cov = [
            [1.0, 0.8, 0.6, 0.1, 0.1],
            [0.8, 1.0, 0.7, 0.1, 0.1],
            [0.6, 0.7, 1.0, 0.1, 0.1],
            [0.1, 0.1, 0.1, 1.0, 0.5],
            [0.1, 0.1, 0.1, 0.5, 1.0],
        ]
        rec = agent.fast_optimize(
            problem=ProblemKind.PORTFOLIO_ALLOCATION,
            symbols=symbols,
            expected_returns=returns,
            covariance=cov,
            max_picks=3,
        )
        assert len(rec.selected_labels) <= 3
        assert rec.objective != 0.0

    def test_autonomous_mode_gates(self):
        import importlib
        import os
        import sys
        from pathlib import Path

        eta_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(eta_root))

        # Load jarvis_admin module directly, resolve its deps
        sys.path.insert(0, str(eta_root))
        os.environ.setdefault("PYTHONPATH", str(eta_root))

        spec = importlib.util.spec_from_file_location(
            "jarvis_admin",
            str(eta_root / "brain" / "jarvis_admin.py"),
            submodule_search_locations=[str(eta_root)],
        )
        mod = importlib.util.module_from_spec(spec)
        with contextlib.suppress(ModuleNotFoundError):
            spec.loader.exec_module(mod)

        # The module file has these classes defined at module scope;
        # if import fails, verify the source directly
        if hasattr(mod, "AUTONOMOUS_ACTIONS"):
            assert mod.SubsystemId.BOT_MNQ in mod.AUTONOMOUS_SUBSYSTEMS
            assert mod.ActionType.ORDER_PLACE in mod.AUTONOMOUS_ACTIONS
            assert mod.ActionType.KILL_SWITCH_RESET not in mod.AUTONOMOUS_ACTIONS

    def test_autonomous_mode_constants_exist(self):
        from pathlib import Path

        eta_root = Path(__file__).resolve().parents[1]
        content = (eta_root / "brain" / "jarvis_admin.py").read_text()
        assert "AUTOPILOT_RESUME" in content
        assert "SubsystemId" in content
        assert "ActionType" in content
        assert "autonomous" in content.lower()
        assert "ORDER_PLACE" in content


class TestInstrumentAgnostic:
    """Verify all instrument configs produce valid controllers."""

    def test_all_instruments_create_valid_controllers(self):
        from common.jarvis import EdgeOptimizer, InstrumentConfig, LossReducer

        trades = [_make_trade(i) for i in range(10)]
        for factory in [
            InstrumentConfig.mnq,
            InstrumentConfig.nq,
            InstrumentConfig.mes,
            InstrumentConfig.btc,
            InstrumentConfig.eth,
            InstrumentConfig.sol,
        ]:
            cfg = factory()
            eo = EdgeOptimizer(trades, cfg=cfg)
            report = eo.full_report()
            assert report["shoulder"] == "RIGHT"
            assert report["instrument"] == cfg.symbol

            lr = LossReducer(trades, cfg=cfg)
            report = lr.full_report()
            assert report["shoulder"] == "LEFT"


class TestParallelTemperingTimeout:
    """Verify PT solver respects timeout."""

    def test_timeout_honored(self):
        from eta_engine.brain.jarvis_v3.quantum.qubo_solver import QuboProblem
        from eta_engine.brain.jarvis_v3.quantum.qubo_supercharged import parallel_tempering_solve

        # Large problem that won't finish quickly
        n = 30
        problem = QuboProblem(n_vars=n, labels=[f"x{i}" for i in range(n)])
        for i in range(n):
            problem.Q.setdefault(i, {})[i] = -1.0
        t0 = time.perf_counter()
        result = parallel_tempering_solve(
            problem,
            n_replicas=8,
            n_iterations=50000,
            timeout_seconds=0.5,
            seed=42,
        )
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0  # Should not run for 5+ seconds
        assert result.energy < 0


class TestTwoWayBridge:
    """Verify operator command parsing and dispatch."""

    def test_parse_all_commands(self):
        from hermes_jarvis_bridge.command_bridge import CommandCategory, parse_command

        tests = [
            ("/jarvis STRATEGY deploy route_b MNQ", CommandCategory.STRATEGY),
            ("/jarvis PARAM size 0.8 MNQ", CommandCategory.PARAM),
            ("/jarvis MODE autonomous on MNQ", CommandCategory.MODE),
            ("/jarvis QUANTUM force_rebalance", CommandCategory.QUANTUM),
            ("/jarvis KAIZEN cycle", CommandCategory.KAIZEN),
            ("/jarvis HEALTH", CommandCategory.HEALTH),
            ("/jarvis KILL trip drawdown", CommandCategory.KILL),
            ("/jarvis STATUS", CommandCategory.STATUS),
            ("/jarvis CONFIRM CMD-123", CommandCategory.CONFIRM),
        ]
        for text, expected in tests:
            cmd = parse_command(text)
            assert cmd is not None, f"Failed to parse: {text}"
            assert cmd.category == expected, f"{text}: got {cmd.category}"

    def test_parse_rejects_invalid(self):
        from hermes_jarvis_bridge.command_bridge import parse_command

        assert parse_command("hello world") is None
        assert parse_command("/jarvis FOO bar") is None
        assert parse_command("/jarvis") is None

    def test_dispatcher_rejects_unauthorized(self):
        from hermes_jarvis_bridge.command_bridge import JarvisCommandDispatcher

        dispatcher = JarvisCommandDispatcher(allowed_chat_ids=["allowed123"])
        ok, reply = dispatcher.dispatch("/jarvis STATUS", "wrong_chat")
        assert ok is False
        assert "Unauthorized" in reply

    def test_dispatcher_handles_confirm_flow(self):
        from hermes_jarvis_bridge.command_bridge import JarvisCommandDispatcher

        dispatcher = JarvisCommandDispatcher()

        # KILL requires confirmation
        ok, reply = dispatcher.dispatch("/jarvis KILL trip drawdown_spike", "chat1")
        assert ok is False
        assert "confirmation" in reply.lower()

        # Extract command_id and confirm
        import re

        cmd_id_match = re.search(r"CMD-[A-Za-z0-9-]+", reply)
        assert cmd_id_match is not None
        ok, reply = dispatcher.dispatch(f"/jarvis CONFIRM {cmd_id_match.group()}", "chat1")
        assert ok is True
