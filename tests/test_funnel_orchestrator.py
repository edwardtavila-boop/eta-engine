"""
EVOLUTIONARY TRADING ALGO  //  tests.test_funnel_orchestrator
=================================================
End-to-end tick coverage for the funnel orchestrator.
"""

from __future__ import annotations

import pytest

from eta_engine.core.sweep_engine import SweepConfig, SweepSplit
from eta_engine.funnel import FunnelOrchestrator, FunnelTickResult
from eta_engine.funnel.equity_monitor import EquityMonitor
from eta_engine.funnel.transfer import (
    TransferRequest,
    TransferResult,
    TransferStatus,
)


def _default_allocator(total_usd: float) -> dict[str, float]:
    """Simple 40/30/15/15 split."""
    if total_usd <= 0.0:
        return {k: 0.0 for k in ("eth", "sol", "xrp", "stable")}
    return {
        "eth": round(total_usd * 0.40, 2),
        "sol": round(total_usd * 0.30, 2),
        "xrp": round(total_usd * 0.15, 2),
        "stable": round(total_usd * 0.15, 2),
    }


def _build_orch() -> FunnelOrchestrator:
    em = EquityMonitor()
    em.register_bot("mnq", baseline=10_000.0)
    em.register_bot("eth_perp", baseline=5_000.0)
    configs = {
        "mnq": SweepConfig(
            bot_name="mnq",
            baseline_usd=10_000.0,
            trigger_multiplier=1.10,
            split=SweepSplit(cold_stake_pct=60.0, reinvest_pct=30.0, reserve_pct=10.0),
        ),
        "eth_perp": SweepConfig(
            bot_name="eth_perp",
            baseline_usd=5_000.0,
            trigger_multiplier=1.10,
            split=SweepSplit(cold_stake_pct=60.0, reinvest_pct=30.0, reserve_pct=10.0),
        ),
    }
    return FunnelOrchestrator(em, configs, allocator=_default_allocator)


class TestOrchestratorTick:
    @pytest.mark.asyncio
    async def test_tick_below_baseline_no_sweep(self) -> None:
        orch = _build_orch()
        result = await orch.tick({"mnq": 9_500.0, "eth_perp": 4_800.0})
        assert isinstance(result, FunnelTickResult)
        assert result.sweeps_triggered == []
        assert result.transfers_queued == []
        assert result.total_swept_usd == 0.0

    @pytest.mark.asyncio
    async def test_tick_above_trigger_produces_60_30_10_split(self) -> None:
        orch = _build_orch()
        # mnq: 12000 vs baseline 10000 → excess 2000. Split 60/30/10 → 1200/600/200.
        result = await orch.tick({"mnq": 12_000.0, "eth_perp": 4_000.0})

        assert len(result.sweeps_triggered) == 1
        s = result.sweeps_triggered[0]
        assert s.bot_name == "mnq"
        assert s.excess_usd == pytest.approx(2000.0, abs=0.01)
        assert s.to_stake == pytest.approx(1200.0, abs=0.01)
        assert s.to_reinvest == pytest.approx(600.0, abs=0.01)
        assert s.to_reserve == pytest.approx(200.0, abs=0.01)

        # Totals in aggregate fields
        assert result.total_swept_usd == pytest.approx(2000.0, abs=0.01)
        assert result.total_to_stake_usd == pytest.approx(1200.0, abs=0.01)
        assert result.total_to_cold_usd == pytest.approx(200.0, abs=0.01)

        # Queued transfers: 4 stake buckets + 1 reinvest + 1 cold = 6
        dests = [t.to_bot for t in result.transfers_queued]
        assert "staking_eth" in dests
        assert "staking_sol" in dests
        assert "staking_xrp" in dests
        assert "staking_stable" in dests
        assert "cold_wallet" in dests
        # Reinvest is from bot to itself
        assert any(t.from_bot == "mnq" and t.to_bot == "mnq" for t in result.transfers_queued)
        # Stake allocator sums to 1200
        stake_total = sum(t.amount_usd for t in result.transfers_queued if t.to_bot.startswith("staking_"))
        assert stake_total == pytest.approx(1200.0, abs=0.02)

    @pytest.mark.asyncio
    async def test_execute_tick_calls_executor_for_every_queued(self) -> None:
        orch = _build_orch()

        calls: list[TransferRequest] = []

        async def fake_executor(req: TransferRequest) -> TransferResult:
            calls.append(req)
            return TransferResult(request=req, status=TransferStatus.EXECUTED, tx_id="t-mock")

        orch.transfer_executor = fake_executor

        tick = await orch.tick({"mnq": 12_000.0, "eth_perp": 5_000.0})
        results = await orch.execute_tick(tick)

        assert len(calls) == len(tick.transfers_queued)
        assert len(results) == len(tick.transfers_queued)
        assert all(r.status == TransferStatus.EXECUTED for r in results)
