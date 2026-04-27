"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_portfolio_rebalancer.

Unit tests for :mod:`eta_engine.strategies.portfolio_rebalancer` --
the bridge that converts :class:`AllocationPlan` (target weights) +
:class:`FunnelSnapshot` (current equity) into concrete transfer
proposals.

Layers covered:

* ``plan_rebalance`` basics -- on-plan vs drifted state
* drift threshold semantics (strict > vs ==)
* min_transfer_usd enforcement
* global-kill skip path
* multi-source / multi-destination greedy pairing
* RebalancePlan.as_dict serialisation
"""

from __future__ import annotations

from eta_engine.funnel.waterfall import (
    FunnelSnapshot,
    LayerId,
    LayerSnapshot,
    VolRegime,
)
from eta_engine.strategies.portfolio_rebalancer import (
    DEFAULT_DRIFT_THRESHOLD_PCT,
    DEFAULT_MIN_TRANSFER_USD,
    RebalancePlan,
    plan_rebalance,
)
from eta_engine.strategies.regime_allocator import AllocationPlan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_TS = "2026-04-17T00:00:00Z"


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


def _balanced_allocation(
    weights: dict[LayerId, float] | None = None,
) -> AllocationPlan:
    if weights is None:
        weights = {
            LayerId.LAYER_1_MNQ: 0.40,
            LayerId.LAYER_2_BTC: 0.30,
            LayerId.LAYER_3_PERPS: 0.20,
            LayerId.LAYER_4_STAKING: 0.10,
        }
    return AllocationPlan(weights=dict(weights))


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


class TestPlanRebalanceBasics:
    def test_returns_rebalance_plan(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 40_000.0,
                LayerId.LAYER_2_BTC: 30_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        assert isinstance(plan, RebalancePlan)

    def test_carries_ts_utc_through(self) -> None:
        snap = _snapshot({LayerId.LAYER_1_MNQ: 100.0})
        plan = plan_rebalance(snap, _balanced_allocation())
        assert plan.ts_utc == _TS

    def test_balanced_portfolio_emits_no_sweeps(self) -> None:
        """Every layer exactly at target -> zero transfers."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 40_000.0,
                LayerId.LAYER_2_BTC: 30_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        assert plan.sweeps == ()
        assert "all_layers_within_drift_threshold" in plan.notes

    def test_total_equity_recorded(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 40_000.0,
                LayerId.LAYER_2_BTC: 30_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        assert plan.total_equity_usd == 100_000.0

    def test_drift_map_populated(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 40_000.0,
                LayerId.LAYER_2_BTC: 30_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        # All four layers on target -> drift pct ~ 0 each
        assert len(plan.drift_pct_by_layer) == 4
        for pct in plan.drift_pct_by_layer.values():
            assert abs(pct) < 1e-9

    def test_target_usd_map_reflects_total_equity(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 40_000.0,
                LayerId.LAYER_2_BTC: 30_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        assert plan.target_usd_by_layer[LayerId.LAYER_1_MNQ] == 40_000.0
        assert plan.target_usd_by_layer[LayerId.LAYER_4_STAKING] == 10_000.0


# ---------------------------------------------------------------------------
# Drift threshold
# ---------------------------------------------------------------------------


class TestDriftThreshold:
    def test_drift_below_threshold_no_sweep(self) -> None:
        """MNQ 2% overweight (below 5% threshold) -> no transfer."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 42_000.0,  # +2%
                LayerId.LAYER_2_BTC: 28_000.0,  # -2%
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        assert plan.sweeps == ()

    def test_drift_above_threshold_emits_sweep(self) -> None:
        """MNQ 10% overweight, BTC 10% underweight -> one transfer."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 50_000.0,  # +10%
                LayerId.LAYER_2_BTC: 20_000.0,  # -10%
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        assert len(plan.sweeps) == 1
        sweep = plan.sweeps[0]
        assert sweep.src is LayerId.LAYER_1_MNQ
        assert sweep.dst is LayerId.LAYER_2_BTC
        assert sweep.amount_usd == 10_000.0

    def test_custom_drift_threshold_looser(self) -> None:
        """20% threshold means a 10% drift does not trigger."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 50_000.0,
                LayerId.LAYER_2_BTC: 20_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(
            snap,
            _balanced_allocation(),
            drift_threshold_pct=0.20,
        )
        assert plan.sweeps == ()

    def test_custom_drift_threshold_tighter(self) -> None:
        """1% threshold means a 2% drift DOES trigger."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 42_000.0,  # +2%
                LayerId.LAYER_2_BTC: 28_000.0,  # -2%
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(
            snap,
            _balanced_allocation(),
            drift_threshold_pct=0.01,
            min_transfer_usd=100.0,
        )
        assert len(plan.sweeps) == 1

    def test_default_threshold_constant(self) -> None:
        assert DEFAULT_DRIFT_THRESHOLD_PCT == 0.05


# ---------------------------------------------------------------------------
# Minimum transfer USD
# ---------------------------------------------------------------------------


class TestMinTransferUsd:
    def test_default_min_transfer_constant(self) -> None:
        assert DEFAULT_MIN_TRANSFER_USD == 100.0

    def test_transfer_below_min_is_skipped(self) -> None:
        """Small total equity + wide drift pct -> small USD -> skip."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 500.0,
                LayerId.LAYER_2_BTC: 200.0,
                LayerId.LAYER_3_PERPS: 200.0,
                LayerId.LAYER_4_STAKING: 100.0,
            }
        )
        plan = plan_rebalance(
            snap,
            _balanced_allocation(),
            min_transfer_usd=1_000.0,
        )
        assert plan.sweeps == ()
        # The skip reason should be recorded in notes.
        assert any("skip" in n or "no_transfers" in n for n in plan.notes)

    def test_transfer_above_min_is_emitted(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 50_000.0,
                LayerId.LAYER_2_BTC: 20_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(
            snap,
            _balanced_allocation(),
            min_transfer_usd=5_000.0,
        )
        assert len(plan.sweeps) == 1
        assert plan.sweeps[0].amount_usd >= 5_000.0


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class TestGlobalKill:
    def test_kill_switch_skips_rebalance(self) -> None:
        """allocation.global_kill_applied=True -> no sweeps, flag set."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 50_000.0,
                LayerId.LAYER_2_BTC: 20_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        alloc = AllocationPlan(
            weights={LayerId.LAYER_4_STAKING: 0.10},
            global_kill_applied=True,
        )
        plan = plan_rebalance(snap, alloc)
        assert plan.sweeps == ()
        assert plan.global_kill_skipped is True
        assert "global_kill_active_rebalance_skipped" in plan.notes


# ---------------------------------------------------------------------------
# Guard paths
# ---------------------------------------------------------------------------


class TestGuardPaths:
    def test_zero_total_equity_returns_empty(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 0.0,
                LayerId.LAYER_4_STAKING: 0.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        assert plan.sweeps == ()
        assert plan.total_equity_usd == 0.0
        assert "zero_total_equity" in plan.notes

    def test_all_weights_zero_returns_empty(self) -> None:
        snap = _snapshot({LayerId.LAYER_1_MNQ: 1_000.0})
        alloc = AllocationPlan(weights={LayerId.LAYER_1_MNQ: 0.0})
        plan = plan_rebalance(snap, alloc)
        assert plan.sweeps == ()
        assert "all_target_weights_zero" in plan.notes


# ---------------------------------------------------------------------------
# Multi-source / multi-destination greedy pairing
# ---------------------------------------------------------------------------


class TestMultiLayerPairing:
    def test_two_over_one_under(self) -> None:
        """Two overweight layers drain into one underweight layer."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 48_000.0,  # +8%
                LayerId.LAYER_2_BTC: 38_000.0,  # +8%
                LayerId.LAYER_3_PERPS: 4_000.0,  # -16%
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        # Both over-weights source transfers into LAYER_3_PERPS
        src_layers = {s.src for s in plan.sweeps}
        dst_layers = {s.dst for s in plan.sweeps}
        assert src_layers == {LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC}
        assert dst_layers == {LayerId.LAYER_3_PERPS}

    def test_one_over_two_under(self) -> None:
        """One overweight layer feeds two underweight layers."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 70_000.0,  # +30%
                LayerId.LAYER_2_BTC: 14_000.0,  # -16%
                LayerId.LAYER_3_PERPS: 6_000.0,  # -14%
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        src_layers = {s.src for s in plan.sweeps}
        dst_layers = {s.dst for s in plan.sweeps}
        assert src_layers == {LayerId.LAYER_1_MNQ}
        # Both underweights received something
        assert LayerId.LAYER_2_BTC in dst_layers
        assert LayerId.LAYER_3_PERPS in dst_layers

    def test_largest_source_paired_with_largest_destination_first(self) -> None:
        """Greedy: biggest overweight -> biggest underweight."""
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 80_000.0,  # +40% (biggest over)
                LayerId.LAYER_2_BTC: 20_000.0,  # -10%
                LayerId.LAYER_3_PERPS: 0.0,  # -20% (biggest under)
                LayerId.LAYER_4_STAKING: 0.0,  # -10%
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        first = plan.sweeps[0]
        assert first.src is LayerId.LAYER_1_MNQ
        assert first.dst is LayerId.LAYER_3_PERPS


# ---------------------------------------------------------------------------
# Reason strings
# ---------------------------------------------------------------------------


class TestReasonStrings:
    def test_reason_identifies_overweight_and_underweight(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 50_000.0,
                LayerId.LAYER_2_BTC: 20_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        reason = plan.sweeps[0].reason
        assert "rebalance:" in reason
        assert "LAYER_1_MNQ_overweight" in reason
        assert "LAYER_2_BTC_underweight" in reason


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_as_dict_basic_keys(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 50_000.0,
                LayerId.LAYER_2_BTC: 20_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        d = plan.as_dict()
        assert d["ts_utc"] == _TS
        assert d["total_equity_usd"] == 100_000.0
        assert isinstance(d["sweeps"], list)
        assert isinstance(d["drift_pct_by_layer"], dict)
        assert isinstance(d["target_usd_by_layer"], dict)
        assert isinstance(d["notes"], list)
        assert d["global_kill_skipped"] is False

    def test_as_dict_sweep_entries(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 50_000.0,
                LayerId.LAYER_2_BTC: 20_000.0,
                LayerId.LAYER_3_PERPS: 20_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, _balanced_allocation())
        d = plan.as_dict()
        assert len(d["sweeps"]) == 1
        sweep = d["sweeps"][0]
        assert sweep["src"] == "LAYER_1_MNQ"
        assert sweep["dst"] == "LAYER_2_BTC"
        assert sweep["amount_usd"] == 10_000.0
        assert "rebalance:" in sweep["reason"]

    def test_as_dict_kill_switch_flag(self) -> None:
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 50_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        alloc = AllocationPlan(
            weights={LayerId.LAYER_4_STAKING: 0.10},
            global_kill_applied=True,
        )
        plan = plan_rebalance(snap, alloc)
        d = plan.as_dict()
        assert d["global_kill_skipped"] is True


# ---------------------------------------------------------------------------
# End-to-end compose regime_allocator -> plan_rebalance
# ---------------------------------------------------------------------------


class TestEndToEndComposition:
    """Verify the bridge consumes a real AllocationPlan from regime_allocator."""

    def test_real_allocator_output_flows_through(self) -> None:
        from eta_engine.strategies.regime_allocator import (
            LayerAllocInput,
            plan_allocation,
        )

        # Let the real allocator produce weights.
        inputs = [
            LayerAllocInput(
                layer=LayerId.LAYER_1_MNQ,
                vol_regime=VolRegime.NORMAL,
            ),
            LayerAllocInput(
                layer=LayerId.LAYER_2_BTC,
                vol_regime=VolRegime.NORMAL,
            ),
            LayerAllocInput(
                layer=LayerId.LAYER_3_PERPS,
                vol_regime=VolRegime.NORMAL,
            ),
            LayerAllocInput(
                layer=LayerId.LAYER_4_STAKING,
                vol_regime=VolRegime.NORMAL,
            ),
        ]
        alloc = plan_allocation(inputs)
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 60_000.0,  # overweight
                LayerId.LAYER_2_BTC: 20_000.0,  # underweight
                LayerId.LAYER_3_PERPS: 15_000.0,
                LayerId.LAYER_4_STAKING: 5_000.0,
            }
        )
        plan = plan_rebalance(snap, alloc)
        # Total equity = 100k; MNQ is ~20k above target -> single sweep.
        assert len(plan.sweeps) >= 1
        assert plan.sweeps[0].src is LayerId.LAYER_1_MNQ

    def test_high_vol_allocator_shrinks_risk_layer_weight(self) -> None:
        """Allocator downweighs risky layer in HIGH vol -> rebalance emits."""
        from eta_engine.strategies.regime_allocator import (
            LayerAllocInput,
            plan_allocation,
        )

        inputs = [
            LayerAllocInput(
                layer=LayerId.LAYER_1_MNQ,
                vol_regime=VolRegime.HIGH,
            ),
            LayerAllocInput(
                layer=LayerId.LAYER_2_BTC,
                vol_regime=VolRegime.NORMAL,
            ),
            LayerAllocInput(
                layer=LayerId.LAYER_3_PERPS,
                vol_regime=VolRegime.NORMAL,
            ),
            LayerAllocInput(
                layer=LayerId.LAYER_4_STAKING,
                vol_regime=VolRegime.NORMAL,
            ),
        ]
        alloc = plan_allocation(inputs)
        # MNQ weight is shrunk by VOL_MULT[HIGH]=0.55.
        mnq_weight = alloc.weights[LayerId.LAYER_1_MNQ]
        btc_weight = alloc.weights[LayerId.LAYER_2_BTC]
        assert mnq_weight < btc_weight, f"HIGH vol should shrink MNQ below BTC; got mnq={mnq_weight}, btc={btc_weight}"

    def test_global_kill_from_allocator_skips_rebalance(self) -> None:
        from eta_engine.strategies.regime_allocator import (
            LayerAllocInput,
            plan_allocation,
        )

        inputs = [
            LayerAllocInput(
                layer=LayerId.LAYER_1_MNQ,
                vol_regime=VolRegime.NORMAL,
            ),
            LayerAllocInput(
                layer=LayerId.LAYER_4_STAKING,
                vol_regime=VolRegime.NORMAL,
            ),
        ]
        alloc = plan_allocation(inputs, global_kill=True)
        snap = _snapshot(
            {
                LayerId.LAYER_1_MNQ: 90_000.0,
                LayerId.LAYER_4_STAKING: 10_000.0,
            }
        )
        plan = plan_rebalance(snap, alloc)
        assert plan.sweeps == ()
        assert plan.global_kill_skipped is True
