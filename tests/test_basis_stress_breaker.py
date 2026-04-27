"""Tests for core.basis_stress_breaker — L3 funding-arb emergency exit gate."""

from __future__ import annotations

from eta_engine.core.basis_stress_breaker import (
    BasisSnapshot,
    BasisStressPolicy,
    StressAction,
    StressReason,
    evaluate_basis_stress,
)


def _clean_snapshot(**overrides) -> BasisSnapshot:
    defaults = dict(
        perp_mid=50_000.0,
        spot_mid=50_000.0,
        perp_margin_distance_usd=5_000.0,  # 50% of notional — well above 15% floor
        spot_margin_distance_usd=5_000.0,
        perp_notional_usd=10_000.0,
        spot_notional_usd=10_000.0,
        basis_history_bps=tuple([5.0] * 29 + [10.0]),  # 30 samples
        stablecoin_peg=1.0,
        perp_venue_reachable=True,
        spot_venue_reachable=True,
    )
    defaults.update(overrides)
    return BasisSnapshot(**defaults)


def test_hold_on_clean_snapshot() -> None:
    decision = evaluate_basis_stress(_clean_snapshot())
    assert decision.action is StressAction.HOLD
    assert decision.reason is StressReason.NONE


def test_both_venues_unreachable_alert_only() -> None:
    snap = _clean_snapshot(perp_venue_reachable=False, spot_venue_reachable=False)
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.ALERT_ONLY
    assert decision.reason is StressReason.EXCHANGE_UNREACHABLE


def test_perp_venue_unreachable_flatten_spot_only() -> None:
    snap = _clean_snapshot(perp_venue_reachable=False)
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.FLATTEN_SPOT_ONLY
    assert decision.reason is StressReason.EXCHANGE_UNREACHABLE


def test_spot_venue_unreachable_flatten_perp_only() -> None:
    snap = _clean_snapshot(spot_venue_reachable=False)
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.FLATTEN_PERP_ONLY
    assert decision.reason is StressReason.EXCHANGE_UNREACHABLE


def test_stablecoin_depeg_flatten_both() -> None:
    snap = _clean_snapshot(stablecoin_peg=0.96)
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.FLATTEN_BOTH
    assert decision.reason is StressReason.STABLECOIN_DEPEG


def test_perp_margin_critical_flatten_both() -> None:
    # 500 / 10000 = 5% — below default 15% safety, triggers flatten.
    snap = _clean_snapshot(perp_margin_distance_usd=400.0)  # 4% ratio
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.FLATTEN_BOTH
    assert decision.reason is StressReason.PERP_MARGIN_CRITICAL


def test_spot_margin_critical_flatten_both() -> None:
    snap = _clean_snapshot(
        perp_margin_distance_usd=5_000.0,
        spot_margin_distance_usd=400.0,
    )
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.FLATTEN_BOTH
    assert decision.reason is StressReason.SPOT_MARGIN_CRITICAL


def test_basis_magnitude_above_3pct_flatten_both() -> None:
    # Perp 3.5% above spot = exceeds 3% threshold.
    snap = _clean_snapshot(perp_mid=51_750.0, spot_mid=50_000.0)
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.FLATTEN_BOTH
    assert decision.reason is StressReason.BASIS_MAGNITUDE


def test_basis_zscore_above_threshold_flatten_both() -> None:
    # Low-jitter ~5bps history + sudden 100bps = z-score > 4.
    # Needs non-zero variance or stdev=0 kills the z-score.
    import random

    rng = random.Random(11)
    prior = tuple(5.0 + rng.gauss(0.0, 0.5) for _ in range(49))
    history = prior + (100.0,)
    snap = _clean_snapshot(basis_history_bps=history)
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.FLATTEN_BOTH
    assert decision.reason is StressReason.BASIS_ZSCORE


def test_basis_zscore_insufficient_samples_no_false_trip() -> None:
    # Only 5 samples — below min_samples floor; shouldn't trip.
    history = tuple([5.0, 10.0, -5.0, 3.0, 100.0])
    snap = _clean_snapshot(basis_history_bps=history)
    decision = evaluate_basis_stress(snap)
    assert decision.action is StressAction.HOLD


def test_priority_unreachable_before_depeg() -> None:
    """Unreachable check runs first — depeg is a moot point if we can't trade."""
    snap = _clean_snapshot(
        perp_venue_reachable=False,
        stablecoin_peg=0.90,
    )
    decision = evaluate_basis_stress(snap)
    assert decision.reason is StressReason.EXCHANGE_UNREACHABLE


def test_priority_depeg_before_margin() -> None:
    """Depeg trips before margin because USD denomination is wrong."""
    snap = _clean_snapshot(
        stablecoin_peg=0.90,
        perp_margin_distance_usd=100.0,  # would trip margin if reached
    )
    decision = evaluate_basis_stress(snap)
    assert decision.reason is StressReason.STABLECOIN_DEPEG


def test_custom_policy_thresholds() -> None:
    policy = BasisStressPolicy(basis_stress_threshold_pct=0.005)  # 0.5%
    snap = _clean_snapshot(perp_mid=50_500.0, spot_mid=50_000.0)  # 1% basis
    decision = evaluate_basis_stress(snap, policy=policy)
    assert decision.action is StressAction.FLATTEN_BOTH
    assert decision.reason is StressReason.BASIS_MAGNITUDE
