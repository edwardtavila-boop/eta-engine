"""
EVOLUTIONARY TRADING ALGO  //  tests.test_funnel_waterfall
==============================================
Unit tests for the pure 4-layer profit-waterfall planner.
"""

from __future__ import annotations

import pytest

from eta_engine.funnel.waterfall import (
    DEFAULT_TIERS,
    TIER_L1_MNQ,
    TIER_L2_BTC,
    TIER_L3_PERPS,
    TIER_L4_STAKING,
    FunnelSnapshot,
    FunnelWaterfall,
    LayerId,
    LayerSnapshot,
    RiskAction,
    VolRegime,
    format_digest,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _layer(
    layer: LayerId,
    *,
    equity: float = 10_000.0,
    peak: float | None = None,
    realized: float = 0.0,
    vol: VolRegime = VolRegime.NORMAL,
    vol_z: float = 0.0,
) -> LayerSnapshot:
    return LayerSnapshot(
        layer=layer,
        current_equity=equity,
        peak_equity=peak if peak is not None else equity,
        realized_pnl_since_last_sweep=realized,
        vol_regime=vol,
        vol_z=vol_z,
    )


def _snap(**overrides: LayerSnapshot) -> FunnelSnapshot:
    defaults: dict[LayerId, LayerSnapshot] = {
        LayerId.LAYER_1_MNQ: _layer(LayerId.LAYER_1_MNQ, equity=50_000.0),
        LayerId.LAYER_2_BTC: _layer(LayerId.LAYER_2_BTC, equity=10_000.0),
        LayerId.LAYER_3_PERPS: _layer(LayerId.LAYER_3_PERPS, equity=5_000.0),
        LayerId.LAYER_4_STAKING: _layer(LayerId.LAYER_4_STAKING, equity=1_000.0),
    }
    for k, v in overrides.items():
        defaults[LayerId[k]] = v
    return FunnelSnapshot(layers=dict(defaults), ts_utc="2026-04-17T12:00:00Z")


# ---------------------------------------------------------------------------
# Tier presets
# ---------------------------------------------------------------------------


def test_default_tier_rules_match_user_brief() -> None:
    assert TIER_L1_MNQ.max_position_pct_per_trade == 0.05
    assert TIER_L1_MNQ.daily_loss_cap_pct == 0.06
    assert TIER_L1_MNQ.drawdown_kill_pct == 0.12
    assert TIER_L1_MNQ.leverage_cap == 10.0
    assert TIER_L1_MNQ.sweep_out_pct == 0.65

    assert TIER_L2_BTC.max_position_pct_per_trade == 0.03
    assert TIER_L2_BTC.daily_loss_cap_pct == 0.04
    assert TIER_L2_BTC.drawdown_kill_pct == 0.09

    assert TIER_L3_PERPS.max_position_pct_per_trade == 0.015
    assert TIER_L3_PERPS.daily_loss_cap_pct == 0.025
    assert TIER_L3_PERPS.drawdown_kill_pct == 0.06
    assert TIER_L3_PERPS.sweep_out_pct == 0.75

    # L4 is a sink
    assert TIER_L4_STAKING.sweep_out_pct == 0.0
    assert TIER_L4_STAKING.max_position_pct_per_trade == 0.0


def test_default_tiers_registry_covers_all_layers() -> None:
    assert set(DEFAULT_TIERS.keys()) == set(LayerId)


# ---------------------------------------------------------------------------
# Snapshot derived properties
# ---------------------------------------------------------------------------


def test_layer_drawdown_pct_clamps_at_zero() -> None:
    # Equity above peak must not produce negative DD
    layer = _layer(LayerId.LAYER_1_MNQ, equity=110.0, peak=100.0)
    assert layer.drawdown_pct == 0.0


def test_layer_drawdown_pct_computed() -> None:
    layer = _layer(LayerId.LAYER_1_MNQ, equity=90.0, peak=100.0)
    assert layer.drawdown_pct == pytest.approx(0.10)


def test_snapshot_totals_aggregate() -> None:
    snap = _snap()
    assert snap.total_equity == pytest.approx(66_000.0)
    assert snap.total_peak == pytest.approx(66_000.0)
    assert snap.global_drawdown_pct == pytest.approx(0.0)


def test_snapshot_global_dd_nonzero() -> None:
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, equity=40_000.0, peak=50_000.0),
    )
    # total = 40k + 10k + 5k + 1k = 56k, peak = 50k + 10k + 5k + 1k = 66k -> 10/66
    assert snap.global_drawdown_pct == pytest.approx(10_000.0 / 66_000.0)


# ---------------------------------------------------------------------------
# Global kill switch
# ---------------------------------------------------------------------------


def test_global_kill_halts_every_layer() -> None:
    # Drop L1 from 50k -> 30k -> global dd = 20/66 ~= 0.303, way above 8%.
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, equity=30_000.0, peak=50_000.0),
    )
    plan = FunnelWaterfall().plan(snap)
    assert plan.global_kill is True
    assert plan.sweeps == []
    halted = {d.layer for d in plan.directives if d.action == RiskAction.HALT}
    assert halted == set(LayerId)
    for d in plan.directives:
        assert d.size_mult == 0.0
        assert d.action == RiskAction.HALT


def test_global_kill_threshold_just_below() -> None:
    # Configure kill_pct to 0.50 so nothing trips and we see clean dict plan
    plan = FunnelWaterfall(global_kill_pct=0.50).plan(_snap())
    assert plan.global_kill is False
    assert plan.directives == []


# ---------------------------------------------------------------------------
# Per-layer DD kill
# ---------------------------------------------------------------------------


def test_single_layer_dd_kill_triggers() -> None:
    # L2 has 9% kill; push peak so DD is exactly 10%
    snap = _snap(
        LAYER_2_BTC=_layer(LayerId.LAYER_2_BTC, equity=9_000.0, peak=10_000.0),
    )
    plan = FunnelWaterfall(global_kill_pct=0.99).plan(snap)
    halted = [d for d in plan.directives if d.action == RiskAction.HALT]
    assert len(halted) == 1
    assert halted[0].layer == LayerId.LAYER_2_BTC
    assert plan.global_kill is False


def test_layer_dd_kill_skips_staking() -> None:
    # L4 has drawdown_kill_pct=0; even big DD shouldn't halt it
    snap = _snap(
        LAYER_4_STAKING=_layer(LayerId.LAYER_4_STAKING, equity=100.0, peak=1_000.0),
    )
    plan = FunnelWaterfall(global_kill_pct=0.99).plan(snap)
    halted = [d for d in plan.directives if d.action == RiskAction.HALT]
    layers = {d.layer for d in halted}
    assert LayerId.LAYER_4_STAKING not in layers


# ---------------------------------------------------------------------------
# Correlation guard
# ---------------------------------------------------------------------------


def test_correlation_guard_fires_on_two_high_vol_layers() -> None:
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, equity=50_000.0, vol=VolRegime.HIGH),
        LAYER_2_BTC=_layer(LayerId.LAYER_2_BTC, equity=10_000.0, vol=VolRegime.HIGH),
    )
    plan = FunnelWaterfall().plan(snap)
    reduce = [d for d in plan.directives if d.action == RiskAction.REDUCE_SIZE]
    layers_cut = {d.layer for d in reduce}
    assert LayerId.LAYER_1_MNQ in layers_cut
    assert LayerId.LAYER_2_BTC in layers_cut
    for d in reduce:
        if d.layer in (LayerId.LAYER_1_MNQ, LayerId.LAYER_2_BTC):
            assert d.size_mult == pytest.approx(0.6)


def test_correlation_guard_skips_when_only_one_high() -> None:
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, equity=50_000.0, vol=VolRegime.HIGH),
    )
    plan = FunnelWaterfall().plan(snap)
    assert not any(d.reason.startswith("correlation_guard") for d in plan.directives)


def test_correlation_guard_does_not_re_cut_halted_layer() -> None:
    # L1 is both in HIGH vol AND drawdown-killed; should only appear as HALT.
    snap = _snap(
        LAYER_1_MNQ=_layer(
            LayerId.LAYER_1_MNQ,
            equity=40_000.0,
            peak=50_000.0,  # 20% DD > 12% kill
            vol=VolRegime.HIGH,
        ),
        LAYER_2_BTC=_layer(LayerId.LAYER_2_BTC, vol=VolRegime.HIGH),
    )
    plan = FunnelWaterfall(global_kill_pct=0.99).plan(snap)
    l1_directives = [d for d in plan.directives if d.layer == LayerId.LAYER_1_MNQ]
    assert len(l1_directives) == 1
    assert l1_directives[0].action == RiskAction.HALT


# ---------------------------------------------------------------------------
# Vol scaling
# ---------------------------------------------------------------------------


def test_vol_scale_reduces_size_with_positive_z() -> None:
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, vol_z=2.0),
    )
    plan = FunnelWaterfall().plan(snap)
    cuts = [d for d in plan.directives if d.layer == LayerId.LAYER_1_MNQ and d.action == RiskAction.REDUCE_SIZE]
    assert len(cuts) == 1
    # 1 / (1 + 2 * 0.5) = 0.5
    assert cuts[0].size_mult == pytest.approx(0.5)


def test_vol_scale_floor_at_quarter() -> None:
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, vol_z=10.0),
    )
    plan = FunnelWaterfall().plan(snap)
    cuts = [d for d in plan.directives if d.layer == LayerId.LAYER_1_MNQ and d.action == RiskAction.REDUCE_SIZE]
    assert cuts[0].size_mult == pytest.approx(0.25)


def test_vol_scale_noop_when_z_zero() -> None:
    plan = FunnelWaterfall().plan(_snap())
    # Nothing should scale when everything is at baseline vol
    vol_cuts = [d for d in plan.directives if d.action == RiskAction.REDUCE_SIZE and "vol_scale" in d.reason]
    assert vol_cuts == []


# ---------------------------------------------------------------------------
# Profit sweeps
# ---------------------------------------------------------------------------


def test_profit_sweep_l1_to_l3() -> None:
    # L1 made $1000 -> sweep 65% = $650 into L3 (min incoming=100 OK)
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, equity=51_000.0, realized=1_000.0),
    )
    plan = FunnelWaterfall().plan(snap)
    l1_sweeps = [s for s in plan.sweeps if s.src == LayerId.LAYER_1_MNQ]
    assert len(l1_sweeps) == 1
    assert l1_sweeps[0].dst == LayerId.LAYER_3_PERPS
    assert l1_sweeps[0].amount_usd == pytest.approx(650.0)


def test_profit_sweep_l3_to_staking() -> None:
    snap = _snap(
        LAYER_3_PERPS=_layer(LayerId.LAYER_3_PERPS, equity=5_500.0, realized=500.0),
    )
    plan = FunnelWaterfall().plan(snap)
    l3_sweeps = [s for s in plan.sweeps if s.src == LayerId.LAYER_3_PERPS]
    assert len(l3_sweeps) == 1
    assert l3_sweeps[0].dst == LayerId.LAYER_4_STAKING
    # 500 * 0.75 = 375 (>= 50 min_incoming for staking)
    assert l3_sweeps[0].amount_usd == pytest.approx(375.0)


def test_profit_sweep_skipped_when_below_min_outgoing() -> None:
    # L1 realized $20 -> 65% = $13, below min_outgoing $25
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, realized=20.0),
    )
    plan = FunnelWaterfall().plan(snap)
    l1_sweeps = [s for s in plan.sweeps if s.src == LayerId.LAYER_1_MNQ]
    assert l1_sweeps == []
    assert any("min_outgoing" in n for n in plan.notes)


def test_profit_sweep_skipped_when_below_min_incoming_of_dest() -> None:
    # L1 realized $100 -> 65% = $65, L3 min_incoming=100 so skip
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, realized=100.0),
    )
    plan = FunnelWaterfall().plan(snap)
    l1_sweeps = [s for s in plan.sweeps if s.src == LayerId.LAYER_1_MNQ]
    assert l1_sweeps == []
    assert any("min_incoming" in n for n in plan.notes)


def test_profit_sweep_skipped_when_src_halted() -> None:
    # L1 is drawdown-killed; any profit sweep from it should be skipped.
    snap = _snap(
        LAYER_1_MNQ=_layer(
            LayerId.LAYER_1_MNQ,
            equity=40_000.0,
            peak=50_000.0,
            realized=1_000.0,
        ),
    )
    plan = FunnelWaterfall(global_kill_pct=0.99).plan(snap)
    l1_sweeps = [s for s in plan.sweeps if s.src == LayerId.LAYER_1_MNQ]
    assert l1_sweeps == []
    assert any("src halted" in n for n in plan.notes)


def test_negative_pnl_never_sweeps() -> None:
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, realized=-500.0),
    )
    plan = FunnelWaterfall().plan(snap)
    assert not any(s.src == LayerId.LAYER_1_MNQ for s in plan.sweeps)


def test_plan_as_dict_is_json_safe() -> None:
    import json as _json

    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, equity=51_000.0, realized=1_000.0),
    )
    plan = FunnelWaterfall().plan(snap)
    as_dict = plan.as_dict()
    # round-trip ensures no non-serializable fields leak
    _json.dumps(as_dict)
    assert as_dict["global_kill"] is False
    assert "ts_utc" in as_dict


# ---------------------------------------------------------------------------
# Digest formatter
# ---------------------------------------------------------------------------


def test_format_digest_renders_sweeps_and_directives() -> None:
    snap = _snap(
        LAYER_1_MNQ=_layer(
            LayerId.LAYER_1_MNQ,
            equity=51_000.0,
            realized=1_000.0,
        ),
    )
    plan = FunnelWaterfall().plan(snap)
    text = format_digest(snap, plan)
    assert "APEX Funnel Digest" in text
    assert "LAYER_1_MNQ" in text
    assert "Sweeps queued" in text
    assert "L1_profit_sweep_to_L3" in text


def test_format_digest_shows_global_kill_banner() -> None:
    snap = _snap(
        LAYER_1_MNQ=_layer(LayerId.LAYER_1_MNQ, equity=30_000.0, peak=50_000.0),
    )
    plan = FunnelWaterfall().plan(snap)
    text = format_digest(snap, plan)
    assert "GLOBAL KILL TRIGGERED" in text
