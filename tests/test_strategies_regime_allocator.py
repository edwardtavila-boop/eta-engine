"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_regime_allocator.

Unit tests for :func:`eta_engine.strategies.regime_allocator.plan_allocation`.

The allocator is a pure function -- deterministic given the same inputs.
Every test reasons about the final normalized weights or the notes
trail.
"""

from __future__ import annotations

import pytest

from eta_engine.funnel.waterfall import LayerId, VolRegime
from eta_engine.strategies.regime_allocator import (
    DEFAULT_BASE,
    VOL_MULT,
    AllocationPlan,
    LayerAllocInput,
    plan_allocation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normal_inputs() -> list[LayerAllocInput]:
    return [
        LayerAllocInput(layer=LayerId.LAYER_1_MNQ),
        LayerAllocInput(layer=LayerId.LAYER_2_BTC),
        LayerAllocInput(layer=LayerId.LAYER_3_PERPS),
        LayerAllocInput(layer=LayerId.LAYER_4_STAKING),
    ]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_base_sums_to_one(self) -> None:
        assert sum(DEFAULT_BASE.values()) == pytest.approx(1.0)

    def test_vol_multipliers_monotonic(self) -> None:
        assert VOL_MULT[VolRegime.LOW] <= VOL_MULT[VolRegime.NORMAL]
        assert VOL_MULT[VolRegime.HIGH] <= VOL_MULT[VolRegime.NORMAL]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPlanAllocationHappy:
    def test_weights_sum_to_one(self) -> None:
        plan = plan_allocation(_normal_inputs())
        assert sum(plan.weights.values()) == pytest.approx(1.0)

    def test_staking_equals_sink_weight(self) -> None:
        plan = plan_allocation(_normal_inputs(), sink_weight=0.15)
        assert plan.weights[LayerId.LAYER_4_STAKING] == pytest.approx(0.15)

    def test_normal_regime_leaves_ratios_intact(self) -> None:
        plan = plan_allocation(_normal_inputs())
        # Starting ratios were 40/30/20 among risky layers (0.9 total)
        risky = {k: v for k, v in plan.weights.items() if k is not LayerId.LAYER_4_STAKING}
        # After normalization they should sum to (1 - 0.10) = 0.90
        assert sum(risky.values()) == pytest.approx(0.90)
        # MNQ (40) should be largest
        assert risky[LayerId.LAYER_1_MNQ] > risky[LayerId.LAYER_2_BTC]
        assert risky[LayerId.LAYER_2_BTC] > risky[LayerId.LAYER_3_PERPS]


# ---------------------------------------------------------------------------
# Vol regime
# ---------------------------------------------------------------------------


class TestVolRegime:
    def test_high_vol_cuts_risky(self) -> None:
        inputs = [
            LayerAllocInput(layer=LayerId.LAYER_1_MNQ, vol_regime=VolRegime.HIGH),
            LayerAllocInput(layer=LayerId.LAYER_2_BTC),
            LayerAllocInput(layer=LayerId.LAYER_3_PERPS),
            LayerAllocInput(layer=LayerId.LAYER_4_STAKING),
        ]
        plan = plan_allocation(inputs)
        # MNQ's share should be smaller than in the all-normal baseline
        baseline = plan_allocation(_normal_inputs())
        assert plan.weights[LayerId.LAYER_1_MNQ] < baseline.weights[LayerId.LAYER_1_MNQ]
        # Check note was recorded (VolRegime.HIGH.value == "HIGH")
        assert any("vol_HIGH" in n for n in plan.notes)

    def test_high_vol_strongly_shrinks_layer(self) -> None:
        inputs = [
            LayerAllocInput(layer=LayerId.LAYER_1_MNQ, vol_regime=VolRegime.HIGH),
            LayerAllocInput(layer=LayerId.LAYER_2_BTC, vol_regime=VolRegime.HIGH),
            LayerAllocInput(layer=LayerId.LAYER_3_PERPS, vol_regime=VolRegime.HIGH),
            LayerAllocInput(layer=LayerId.LAYER_4_STAKING),
        ]
        plan = plan_allocation(inputs)
        # Weights still sum to 1 and the sink is preserved at its floor
        assert sum(plan.weights.values()) == pytest.approx(1.0)
        assert plan.weights[LayerId.LAYER_4_STAKING] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Correlation penalty
# ---------------------------------------------------------------------------


class TestCorrelationPenalty:
    def test_penalizes_smaller_weight_when_corr_high(self) -> None:
        # BTC starts at 0.30, PERPS at 0.20 -> PERPS should be penalized
        plan = plan_allocation(
            _normal_inputs(),
            correlations={(LayerId.LAYER_2_BTC, LayerId.LAYER_3_PERPS): 0.90},
        )
        assert plan.corr_penalty_applied
        baseline = plan_allocation(_normal_inputs())
        assert plan.weights[LayerId.LAYER_3_PERPS] < baseline.weights[LayerId.LAYER_3_PERPS]

    def test_no_penalty_below_threshold(self) -> None:
        plan = plan_allocation(
            _normal_inputs(),
            correlations={(LayerId.LAYER_2_BTC, LayerId.LAYER_3_PERPS): 0.50},
            corr_threshold=0.75,
        )
        assert not plan.corr_penalty_applied

    def test_ignores_staking_pair(self) -> None:
        plan = plan_allocation(
            _normal_inputs(),
            correlations={(LayerId.LAYER_2_BTC, LayerId.LAYER_4_STAKING): 0.95},
        )
        assert not plan.corr_penalty_applied


# ---------------------------------------------------------------------------
# Realized-edge boost
# ---------------------------------------------------------------------------


class TestEdgeBoost:
    def test_edge_boost_increases_weight(self) -> None:
        boosted = plan_allocation(
            [
                LayerAllocInput(layer=LayerId.LAYER_1_MNQ, realized_edge=1.0),
                LayerAllocInput(layer=LayerId.LAYER_2_BTC),
                LayerAllocInput(layer=LayerId.LAYER_3_PERPS),
                LayerAllocInput(layer=LayerId.LAYER_4_STAKING),
            ],
        )
        baseline = plan_allocation(_normal_inputs())
        assert boosted.weights[LayerId.LAYER_1_MNQ] > baseline.weights[LayerId.LAYER_1_MNQ]

    def test_zero_edge_no_change(self) -> None:
        boosted = plan_allocation(_normal_inputs())
        baseline = plan_allocation(_normal_inputs())
        assert boosted.weights == baseline.weights


# ---------------------------------------------------------------------------
# Global kill
# ---------------------------------------------------------------------------


class TestGlobalKill:
    def test_global_kill_zeros_risky_layers(self) -> None:
        plan = plan_allocation(_normal_inputs(), global_kill=True)
        assert plan.weights[LayerId.LAYER_1_MNQ] == 0.0
        assert plan.weights[LayerId.LAYER_2_BTC] == 0.0
        assert plan.weights[LayerId.LAYER_3_PERPS] == 0.0
        # Staking sink still gets its floor
        assert plan.weights[LayerId.LAYER_4_STAKING] == pytest.approx(0.10)
        assert plan.global_kill_applied
        assert any("global_kill" in n for n in plan.notes)


# ---------------------------------------------------------------------------
# as_dict
# ---------------------------------------------------------------------------


class TestAllocationPlanDict:
    def test_as_dict_is_json_safe(self) -> None:
        plan = plan_allocation(_normal_inputs())
        d = plan.as_dict()
        assert "weights" in d
        # All keys should be layer.value strings
        for k in d["weights"]:
            assert isinstance(k, str)
        assert isinstance(d["notes"], list)
        assert isinstance(d["corr_penalty_applied"], bool)
        assert isinstance(d["global_kill_applied"], bool)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_all_risky_zero_keeps_sink(self) -> None:
        inputs = [
            LayerAllocInput(layer=LayerId.LAYER_4_STAKING),
        ]
        plan = plan_allocation(inputs)
        assert plan.weights[LayerId.LAYER_4_STAKING] == pytest.approx(0.10)
        assert any("risky_sum_zero" in n for n in plan.notes)

    def test_custom_base_respected(self) -> None:
        custom = {
            LayerId.LAYER_1_MNQ: 0.50,
            LayerId.LAYER_2_BTC: 0.20,
            LayerId.LAYER_3_PERPS: 0.20,
            LayerId.LAYER_4_STAKING: 0.10,
        }
        plan = plan_allocation(_normal_inputs(), base=custom)
        # MNQ should have the highest share
        risky = {k: v for k, v in plan.weights.items() if k is not LayerId.LAYER_4_STAKING}
        assert risky[LayerId.LAYER_1_MNQ] == max(risky.values())

    def test_plan_is_frozen(self) -> None:
        plan = plan_allocation(_normal_inputs())
        assert isinstance(plan, AllocationPlan)
        with pytest.raises(AttributeError):
            plan.global_kill_applied = True  # type: ignore[misc]
