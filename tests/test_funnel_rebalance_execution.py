"""EVOLUTIONARY TRADING ALGO  //  tests.test_funnel_rebalance_execution.

Coverage for the bridge that converts a :class:`RebalancePlan` into
concrete :class:`TransferRequest` rows and the
:meth:`FunnelOrchestrator.execute_rebalance` channel that routes them
through the transfer executor.

Three layers:

* ``rebalance_plan_to_transfers`` -- pure converter invariants.
* ``FunnelOrchestrator.execute_rebalance`` -- executor wiring, missing-
  mapping behaviour, policy rejection via TransferManager.
* End-to-end -- real ``plan_rebalance`` output flowing through the
  orchestrator, including kill-switch deference.
"""

from __future__ import annotations

import pytest

from eta_engine.funnel import FunnelOrchestrator
from eta_engine.funnel.equity_monitor import EquityMonitor
from eta_engine.funnel.transfer import (
    DryRunExecutor,
    StubExecutor,
    TransferManager,
    TransferPolicy,
    TransferRequest,
    TransferResult,
    TransferStatus,
)
from eta_engine.funnel.waterfall import (
    FunnelSnapshot,
    LayerId,
    LayerSnapshot,
    ProposedSweep,
    VolRegime,
)
from eta_engine.strategies.portfolio_rebalancer import (
    RebalancePlan,
    plan_rebalance,
    rebalance_plan_to_transfers,
)
from eta_engine.strategies.regime_allocator import AllocationPlan

_TS = "2026-04-17T00:00:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _layer(layer_id: LayerId, equity: float) -> LayerSnapshot:
    return LayerSnapshot(
        layer=layer_id,
        current_equity=equity,
        peak_equity=max(equity, 1.0),
        realized_pnl_since_last_sweep=0.0,
        vol_regime=VolRegime.NORMAL,
    )


def _snapshot(equities: dict[LayerId, float]) -> FunnelSnapshot:
    return FunnelSnapshot(
        layers={layer: _layer(layer, eq) for layer, eq in equities.items()},
        ts_utc=_TS,
    )


def _full_layer_map() -> dict[LayerId, str]:
    return {
        LayerId.LAYER_1_MNQ: "mnq_apex",
        LayerId.LAYER_2_BTC: "btc_core",
        LayerId.LAYER_3_PERPS: "perps_heat",
        LayerId.LAYER_4_STAKING: "staking_main",
    }


def _plan_with_sweeps(*sweeps: ProposedSweep) -> RebalancePlan:
    return RebalancePlan(
        ts_utc=_TS,
        total_equity_usd=100_000.0,
        sweeps=tuple(sweeps),
    )


def _sweep(
    src: LayerId,
    dst: LayerId,
    amount_usd: float,
    reason: str = "",
) -> ProposedSweep:
    return ProposedSweep(
        src=src,
        dst=dst,
        amount_usd=amount_usd,
        reason=reason or f"rebalance:{src.value}_overweight_to_{dst.value}_underweight",
    )


def _orch_with_executor(
    executor: object,
    *,
    allocator: object | None = None,
) -> FunnelOrchestrator:
    em = EquityMonitor()
    # Rebalance path ignores sweep configs / equity monitor state, so an
    # empty wiring is fine; we just need an orchestrator that will call
    # our executor when execute_rebalance fires.
    return FunnelOrchestrator(
        equity_monitor=em,
        sweep_configs={},
        allocator=allocator or (lambda _usd: {}),
        transfer_executor=executor.execute if hasattr(executor, "execute") else executor,
    )


# ---------------------------------------------------------------------------
# Pure converter
# ---------------------------------------------------------------------------


class TestRebalancePlanToTransfers:
    def test_empty_plan_returns_empty_list(self) -> None:
        plan = _plan_with_sweeps()
        out = rebalance_plan_to_transfers(plan, _full_layer_map())
        assert out == []

    def test_single_sweep_produces_single_request(self) -> None:
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
        )
        out = rebalance_plan_to_transfers(plan, _full_layer_map())
        assert len(out) == 1
        assert isinstance(out[0], TransferRequest)

    def test_from_and_to_bot_mapped_via_layer_map(self) -> None:
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 1_234.56),
        )
        out = rebalance_plan_to_transfers(plan, _full_layer_map())
        assert out[0].from_bot == "mnq_apex"
        assert out[0].to_bot == "staking_main"

    def test_amount_rounded_to_two_decimals(self) -> None:
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 123.456789),
        )
        out = rebalance_plan_to_transfers(plan, _full_layer_map())
        assert out[0].amount_usd == 123.46

    def test_reason_propagated_from_sweep(self) -> None:
        reason = "rebalance:layer_1_mnq_overweight_to_layer_4_staking_underweight"
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 500.0, reason),
        )
        out = rebalance_plan_to_transfers(plan, _full_layer_map())
        assert out[0].reason == reason

    def test_requires_approval_defaults_false(self) -> None:
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
        )
        out = rebalance_plan_to_transfers(plan, _full_layer_map())
        assert out[0].requires_approval is False

    def test_missing_source_layer_skips_sweep(self) -> None:
        layer_map = {
            LayerId.LAYER_2_BTC: "btc_core",
            LayerId.LAYER_4_STAKING: "staking_main",
        }
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
        )
        out = rebalance_plan_to_transfers(plan, layer_map)
        assert out == []

    def test_missing_destination_layer_skips_sweep(self) -> None:
        layer_map = {
            LayerId.LAYER_1_MNQ: "mnq_apex",
            LayerId.LAYER_2_BTC: "btc_core",
        }
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 500.0),
        )
        out = rebalance_plan_to_transfers(plan, layer_map)
        assert out == []

    def test_order_preserved_from_sweeps(self) -> None:
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 500.0),
            _sweep(LayerId.LAYER_2_BTC, LayerId.LAYER_3_PERPS, 200.0),
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_3_PERPS, 150.0),
        )
        out = rebalance_plan_to_transfers(plan, _full_layer_map())
        assert [(r.from_bot, r.to_bot) for r in out] == [
            ("mnq_apex", "staking_main"),
            ("btc_core", "perps_heat"),
            ("mnq_apex", "perps_heat"),
        ]

    def test_gap_detection_via_length_diff(self) -> None:
        # one sweep maps, one does not
        layer_map = {
            LayerId.LAYER_1_MNQ: "mnq_apex",
            LayerId.LAYER_2_BTC: "btc_core",
        }
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),  # ok
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 200.0),  # skip
        )
        out = rebalance_plan_to_transfers(plan, layer_map)
        assert len(out) == 1
        assert len(plan.sweeps) == 2
        assert len(plan.sweeps) - len(out) == 1  # gap count

    def test_multiple_sweeps_same_layer_all_preserved(self) -> None:
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_3_PERPS, 300.0),
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 200.0),
        )
        out = rebalance_plan_to_transfers(plan, _full_layer_map())
        assert len(out) == 3
        assert all(r.from_bot == "mnq_apex" for r in out)


# ---------------------------------------------------------------------------
# Orchestrator.execute_rebalance
# ---------------------------------------------------------------------------


class TestOrchestratorExecuteRebalance:
    @pytest.mark.asyncio
    async def test_empty_plan_no_executor_calls(self) -> None:
        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        plan = _plan_with_sweeps()
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert results == []
        assert executor.calls == []

    @pytest.mark.asyncio
    async def test_happy_path_executes_all_requests(self) -> None:
        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 200.0),
        )
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert len(results) == 2
        assert all(isinstance(r, TransferResult) for r in results)
        assert len(executor.calls) == 2

    @pytest.mark.asyncio
    async def test_executor_receives_mapped_bot_names(self) -> None:
        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 500.0),
        )
        await orch.execute_rebalance(plan, _full_layer_map())
        assert executor.calls[0].from_bot == "mnq_apex"
        assert executor.calls[0].to_bot == "staking_main"

    @pytest.mark.asyncio
    async def test_result_count_matches_sweep_count_when_fully_mapped(self) -> None:
        executor = StubExecutor()
        orch = _orch_with_executor(executor)
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_3_PERPS, 200.0),
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 300.0),
        )
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert len(results) == len(plan.sweeps)
        # StubExecutor tags every result EXECUTED
        assert all(r.status == TransferStatus.EXECUTED for r in results)

    @pytest.mark.asyncio
    async def test_missing_layer_map_skips_and_no_executor_call(self) -> None:
        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        layer_map = {
            LayerId.LAYER_1_MNQ: "mnq_apex",
            LayerId.LAYER_2_BTC: "btc_core",
        }
        plan = _plan_with_sweeps(
            # LAYER_4_STAKING not mapped -> skipped
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 500.0),
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 200.0),
        )
        results = await orch.execute_rebalance(plan, layer_map)
        assert len(results) == 1  # only the mapped sweep executed
        assert executor.calls[0].to_bot == "btc_core"

    @pytest.mark.asyncio
    async def test_stub_executor_returns_executed_status(self) -> None:
        executor = StubExecutor(fee_usd=0.25)
        orch = _orch_with_executor(executor)
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
        )
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert results[0].status == TransferStatus.EXECUTED
        assert results[0].fee_usd == 0.25
        assert results[0].tx_id is not None

    @pytest.mark.asyncio
    async def test_manager_policy_rejection_returns_failed(self) -> None:
        # Whitelist only allows mnq_apex -> btc_core; a rebalance aimed at
        # staking_main must be rejected.
        policy = TransferPolicy(
            per_txn_limit_usd=10_000.0,
            daily_limit_usd=50_000.0,
            approval_threshold_usd=50_000.0,  # out of the way
            whitelist={"mnq_apex": {"btc_core"}},
        )
        manager = TransferManager(policy=policy, executor=StubExecutor())
        orch = FunnelOrchestrator(
            equity_monitor=EquityMonitor(),
            sweep_configs={},
            allocator=lambda _usd: {},
            transfer_executor=manager.execute,
        )
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_4_STAKING, 500.0),
        )
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert len(results) == 1
        assert results[0].status == TransferStatus.FAILED
        assert "whitelist" in (results[0].error or "").lower()
        # Ledger records the REJECTED outcome
        entries = manager.ledger.entries()
        assert len(entries) == 1
        assert entries[0].outcome == "REJECTED"

    @pytest.mark.asyncio
    async def test_manager_approval_gate_rejects_large_unflagged_transfer(self) -> None:
        # Whitelist permissive, but approval threshold fires on 10k+ rebalance.
        policy = TransferPolicy(
            per_txn_limit_usd=100_000.0,
            daily_limit_usd=500_000.0,
            approval_threshold_usd=10_000.0,
        )
        manager = TransferManager(policy=policy, executor=StubExecutor())
        orch = FunnelOrchestrator(
            equity_monitor=EquityMonitor(),
            sweep_configs={},
            allocator=lambda _usd: {},
            transfer_executor=manager.execute,
        )
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 15_000.0),
        )
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert results[0].status == TransferStatus.FAILED
        assert "approval" in (results[0].error or "").lower()

    @pytest.mark.asyncio
    async def test_dry_run_executor_status_is_approved(self) -> None:
        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
        )
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert results[0].status == TransferStatus.APPROVED
        assert results[0].tx_id is None


# ---------------------------------------------------------------------------
# End-to-end: plan_rebalance output -> orchestrator execution
# ---------------------------------------------------------------------------


class TestRebalanceExecutionEndToEnd:
    @pytest.mark.asyncio
    async def test_drifted_snapshot_executes_expected_sweeps(self) -> None:
        # MNQ is 30k over target (40k target, 70k actual). Both PERPS
        # and STAKING sit strictly below the 5% threshold, so MNQ excess
        # is paired with them greedily.
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 70_000.0,
                LayerId.LAYER_2_BTC: 26_000.0,
                LayerId.LAYER_3_PERPS: 2_000.0,
                LayerId.LAYER_4_STAKING: 2_000.0,
            }
        )
        alloc = AllocationPlan(
            weights={
                LayerId.LAYER_1_MNQ: 0.40,
                LayerId.LAYER_2_BTC: 0.30,
                LayerId.LAYER_3_PERPS: 0.20,
                LayerId.LAYER_4_STAKING: 0.10,
            }
        )
        plan = plan_rebalance(snap, alloc)
        assert plan.sweeps, "expected at least one sweep for drifted snapshot"

        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert len(results) == len(plan.sweeps)
        # Every executed transfer originates from the overweight MNQ layer
        assert all(call.from_bot == "mnq_apex" for call in executor.calls)

    @pytest.mark.asyncio
    async def test_on_plan_snapshot_produces_no_executor_calls(self) -> None:
        # Layers sit exactly on target -> empty plan.
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 40_000.0,
                LayerId.LAYER_2_BTC: 30_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        alloc = AllocationPlan(
            weights={
                LayerId.LAYER_1_MNQ: 0.40,
                LayerId.LAYER_2_BTC: 0.30,
                LayerId.LAYER_3_PERPS: 0.20,
                LayerId.LAYER_4_STAKING: 0.10,
            }
        )
        plan = plan_rebalance(snap, alloc)
        assert plan.sweeps == ()

        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert results == []
        assert executor.calls == []

    @pytest.mark.asyncio
    async def test_kill_switch_plan_produces_no_executor_calls(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 70_000.0,
                LayerId.LAYER_2_BTC: 26_000.0,
                LayerId.LAYER_3_PERPS: 2_000.0,
                LayerId.LAYER_4_STAKING: 2_000.0,
            }
        )
        alloc = AllocationPlan(
            weights={
                LayerId.LAYER_1_MNQ: 0.0,
                LayerId.LAYER_2_BTC: 0.0,
                LayerId.LAYER_3_PERPS: 0.0,
                LayerId.LAYER_4_STAKING: 1.0,
            },
            global_kill_applied=True,
        )
        plan = plan_rebalance(snap, alloc)
        assert plan.global_kill_skipped is True
        assert plan.sweeps == ()

        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        results = await orch.execute_rebalance(plan, _full_layer_map())
        # Nothing executed -- kill-switch unwind is the waterfall's job.
        assert results == []
        assert executor.calls == []

    @pytest.mark.asyncio
    async def test_partial_layer_map_executes_only_reachable_sweeps(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 70_000.0,
                LayerId.LAYER_2_BTC: 26_000.0,
                LayerId.LAYER_3_PERPS: 2_000.0,
                LayerId.LAYER_4_STAKING: 2_000.0,
            }
        )
        alloc = AllocationPlan(
            weights={
                LayerId.LAYER_1_MNQ: 0.40,
                LayerId.LAYER_2_BTC: 0.30,
                LayerId.LAYER_3_PERPS: 0.20,
                LayerId.LAYER_4_STAKING: 0.10,
            }
        )
        plan = plan_rebalance(snap, alloc)
        # Drop STAKING from the layer map -> any sweep into staking is
        # silently dropped, but sweeps into perps still flow.
        partial_map = {
            LayerId.LAYER_1_MNQ: "mnq_apex",
            LayerId.LAYER_2_BTC: "btc_core",
            LayerId.LAYER_3_PERPS: "perps_heat",
        }
        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        results = await orch.execute_rebalance(plan, partial_map)
        assert len(results) <= len(plan.sweeps)
        # Every executed transfer went to a mapped layer (never "staking_main")
        assert all(call.to_bot != "staking_main" for call in executor.calls)
        assert all(call.to_bot in partial_map.values() for call in executor.calls)

    @pytest.mark.asyncio
    async def test_zero_equity_plan_produces_no_executor_calls(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 0.0,
                LayerId.LAYER_2_BTC: 0.0,
                LayerId.LAYER_3_PERPS: 0.0,
                LayerId.LAYER_4_STAKING: 0.0,
            }
        )
        alloc = AllocationPlan(
            weights={
                LayerId.LAYER_1_MNQ: 0.40,
                LayerId.LAYER_2_BTC: 0.30,
                LayerId.LAYER_3_PERPS: 0.20,
                LayerId.LAYER_4_STAKING: 0.10,
            }
        )
        plan = plan_rebalance(snap, alloc)
        assert "zero_total_equity" in plan.notes

        executor = DryRunExecutor()
        orch = _orch_with_executor(executor)
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert results == []
        assert executor.calls == []


# ---------------------------------------------------------------------------
# Executor override semantics (rebalance reuses the same injection point
# as profit sweeps, so swapping executors is a one-liner)
# ---------------------------------------------------------------------------


class TestExecutorInjection:
    @pytest.mark.asyncio
    async def test_orchestrator_calls_transfer_executor_callable(self) -> None:
        captured: list[TransferRequest] = []

        async def capture(req: TransferRequest) -> TransferResult:
            captured.append(req)
            return TransferResult(request=req, status=TransferStatus.EXECUTED)

        orch = FunnelOrchestrator(
            equity_monitor=EquityMonitor(),
            sweep_configs={},
            allocator=lambda _usd: {},
            transfer_executor=capture,
        )
        plan = _plan_with_sweeps(
            _sweep(LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC, 500.0),
        )
        results = await orch.execute_rebalance(plan, _full_layer_map())
        assert len(captured) == 1
        assert captured[0].from_bot == "mnq_apex"
        assert results[0].status == TransferStatus.EXECUTED
