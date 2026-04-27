"""Regime classifier + drift detector tests — P10_AI regime_model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eta_engine.brain.regime import (
    RegimeAxes,
    RegimeType,
    classify_regime,
    detect_drift,
)

# ---------------------------------------------------------------------------
# RegimeAxes validation
# ---------------------------------------------------------------------------


def test_regime_axes_accepts_boundary_values() -> None:
    axes = RegimeAxes(vol=0.0, trend=-1.0, liquidity=0.0, correlation=0.0, macro="neutral")
    assert axes.vol == 0.0
    assert axes.trend == -1.0

    axes = RegimeAxes(vol=1.0, trend=1.0, liquidity=1.0, correlation=1.0, macro="hawkish")
    assert axes.vol == 1.0
    assert axes.trend == 1.0


def test_regime_axes_rejects_vol_out_of_range() -> None:
    with pytest.raises(ValidationError):
        RegimeAxes(vol=1.5, trend=0.0, liquidity=0.5, correlation=0.5, macro="neutral")


def test_regime_axes_rejects_trend_out_of_range() -> None:
    with pytest.raises(ValidationError):
        RegimeAxes(vol=0.5, trend=-1.5, liquidity=0.5, correlation=0.5, macro="neutral")


def test_regime_axes_rejects_correlation_above_one() -> None:
    with pytest.raises(ValidationError):
        RegimeAxes(vol=0.5, trend=0.0, liquidity=0.5, correlation=2.0, macro="neutral")


# ---------------------------------------------------------------------------
# classify_regime — decision-tree priority order
# ---------------------------------------------------------------------------


def test_classify_crisis_by_macro_label_wins_over_everything() -> None:
    # Even with otherwise-benign numbers, macro=crisis short-circuits to CRISIS
    axes = RegimeAxes(vol=0.3, trend=0.1, liquidity=0.8, correlation=0.4, macro="crisis")
    assert classify_regime(axes) == RegimeType.CRISIS


def test_classify_crisis_by_vol_spike_plus_liquidity_drought() -> None:
    # vol>0.85 AND liquidity<0.2 → CRISIS even when macro isn't "crisis"
    axes = RegimeAxes(vol=0.9, trend=0.0, liquidity=0.1, correlation=0.5, macro="neutral")
    assert classify_regime(axes) == RegimeType.CRISIS


def test_classify_high_vol_requires_both_vol_and_correlation() -> None:
    axes = RegimeAxes(vol=0.8, trend=0.0, liquidity=0.5, correlation=0.8, macro="neutral")
    assert classify_regime(axes) == RegimeType.HIGH_VOL


def test_classify_high_vol_not_triggered_by_vol_alone() -> None:
    # vol>0.7 but corr<=0.7 does not fire HIGH_VOL
    axes = RegimeAxes(vol=0.8, trend=0.0, liquidity=0.5, correlation=0.5, macro="neutral")
    assert classify_regime(axes) != RegimeType.HIGH_VOL


def test_classify_low_vol_requires_low_trend_absolute() -> None:
    axes = RegimeAxes(vol=0.1, trend=0.1, liquidity=0.5, correlation=0.5, macro="neutral")
    assert classify_regime(axes) == RegimeType.LOW_VOL


def test_classify_trending_up_when_trend_strong_and_vol_medium() -> None:
    axes = RegimeAxes(vol=0.4, trend=0.7, liquidity=0.5, correlation=0.5, macro="neutral")
    assert classify_regime(axes) == RegimeType.TRENDING


def test_classify_trending_down_when_trend_strong_negative() -> None:
    axes = RegimeAxes(vol=0.4, trend=-0.8, liquidity=0.5, correlation=0.5, macro="neutral")
    assert classify_regime(axes) == RegimeType.TRENDING


def test_classify_ranging_when_trend_small_and_vol_mid_low() -> None:
    axes = RegimeAxes(vol=0.3, trend=0.1, liquidity=0.5, correlation=0.5, macro="neutral")
    assert classify_regime(axes) == RegimeType.RANGING


def test_classify_transition_is_catch_all() -> None:
    # vol=0.6, trend=0.4 (not >0.5), not ranging (vol out of [0.2,0.5])
    axes = RegimeAxes(vol=0.6, trend=0.4, liquidity=0.5, correlation=0.3, macro="neutral")
    assert classify_regime(axes) == RegimeType.TRANSITION


def test_classify_crisis_priority_over_high_vol() -> None:
    # Both HIGH_VOL (vol>0.7 + corr>0.7) and CRISIS (vol>0.85 + liq<0.2) could fire.
    # CRISIS wins by priority.
    axes = RegimeAxes(vol=0.95, trend=0.0, liquidity=0.05, correlation=0.95, macro="neutral")
    assert classify_regime(axes) == RegimeType.CRISIS


# ---------------------------------------------------------------------------
# detect_drift
# ---------------------------------------------------------------------------


def test_detect_drift_false_on_single_regime_history() -> None:
    assert detect_drift([RegimeType.TRENDING]) is False


def test_detect_drift_false_when_current_matches_mode() -> None:
    history = [RegimeType.TRENDING] * 10 + [RegimeType.TRENDING]
    assert detect_drift(history) is False


def test_detect_drift_true_when_current_breaks_from_mode() -> None:
    history = [RegimeType.TRENDING] * 10 + [RegimeType.CRISIS]
    assert detect_drift(history) is True


def test_detect_drift_respects_window_size() -> None:
    # Old mode was LOW_VOL but window=5 only sees recent HIGH_VOL bias
    old = [RegimeType.LOW_VOL] * 15
    recent = [RegimeType.HIGH_VOL] * 4 + [RegimeType.HIGH_VOL]
    history = old + recent
    # Within last 5 the mode is HIGH_VOL and current is HIGH_VOL → no drift
    assert detect_drift(history, window=5) is False


def test_detect_drift_empty_history_returns_false() -> None:
    assert detect_drift([]) is False
