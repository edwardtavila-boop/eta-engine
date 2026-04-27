"""Ghost-trader stop-hunt simulator tests — P3_PROOF adversarial."""

from __future__ import annotations

import numpy as np
import pytest

from eta_engine.backtest.stop_hunt_sim import (
    Position,
    StopHuntPolicy,
    simulate,
)


def _long_position(entry: float = 100.0, stop: float = 98.0) -> Position:
    return Position(
        symbol="MNQ",
        side="long",
        entry_price=entry,
        stop_price=stop,
        size_contracts=2.0,
        point_value_usd=2.0,
    )


def _short_position(entry: float = 100.0, stop: float = 102.0) -> Position:
    return Position(
        symbol="NQ",
        side="short",
        entry_price=entry,
        stop_price=stop,
        size_contracts=1.0,
        point_value_usd=20.0,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_shape_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="shape mismatch"):
        simulate([_long_position()], np.zeros(10), np.zeros(5))


def test_non_1d_bars_rejected() -> None:
    with pytest.raises(ValueError, match="1D"):
        simulate([_long_position()], np.zeros((5, 2)), np.zeros((5, 2)))


# ---------------------------------------------------------------------------
# Long-side
# ---------------------------------------------------------------------------


def test_long_untouched_when_bars_stay_above_stop() -> None:
    # Bars never dip to 98 - penetration
    highs = np.full(50, 100.5)
    lows = np.full(50, 99.0)  # stays above stop at 98
    report = simulate([_long_position()], highs, lows)
    assert report.positions[0].hunted is False
    assert report.positions[0].pnl_usd == 0.0
    assert report.hunt_hit_rate == 0.0
    assert report.robustness_score == 1.0


def test_long_hunted_when_bar_penetrates_stop() -> None:
    highs = np.full(50, 100.5)
    lows = np.full(50, 99.0)
    lows[10] = 97.5  # dips below stop - penetration
    report = simulate([_long_position()], highs, lows)
    pos = report.positions[0]
    assert pos.hunted is True
    # Fill = 98 - 0.25 (1 tick overshoot) - 0.25 (1 tick spread) = 97.50
    assert pos.hunt_fill_price == pytest.approx(97.5, abs=1e-4)
    # PnL = (97.5 - 100) * 2 contracts * 2 usd/pt = -10 usd
    assert pos.pnl_usd == pytest.approx(-10.0, abs=0.01)
    assert report.total_pnl_usd == pytest.approx(-10.0, abs=0.01)


# ---------------------------------------------------------------------------
# Short-side
# ---------------------------------------------------------------------------


def test_short_hunted_when_bar_spikes_above_stop() -> None:
    highs = np.full(50, 101.0)
    lows = np.full(50, 99.5)
    highs[5] = 102.5  # spikes above short stop at 102
    report = simulate([_short_position()], highs, lows)
    pos = report.positions[0]
    assert pos.hunted is True
    # Fill = 102 + 0.25 + 0.25 = 102.50
    assert pos.hunt_fill_price == pytest.approx(102.5, abs=1e-4)
    # PnL = (100 - 102.5) * 1 * 20 = -50 usd
    assert pos.pnl_usd == pytest.approx(-50.0, abs=0.01)


# ---------------------------------------------------------------------------
# Policy knobs
# ---------------------------------------------------------------------------


def test_penetration_ticks_widens_the_trigger_band() -> None:
    # With penetration=1, bars at 97.8 are INSIDE the allowed band (98-0.25=97.75)
    highs = np.full(20, 100.0)
    lows = np.full(20, 97.8)
    shallow = simulate([_long_position()], highs, lows)
    assert shallow.positions[0].hunted is False
    # With penetration=2 ticks (0.50), trigger = 97.50, so 97.8 still doesn't reach
    deep = simulate(
        [_long_position()],
        highs,
        lows,
        policy=StopHuntPolicy(penetration_ticks=0.1),  # even shallower
    )
    # Trigger = 98 - 0.025 = 97.975. 97.8 < 97.975 → hunted.
    assert deep.positions[0].hunted is True


def test_hit_rate_reflects_fraction_hunted() -> None:
    # Two positions, only one gets hit.
    highs = np.full(20, 100.5)
    lows = np.full(20, 99.0)
    lows[3] = 97.5  # long gets hit
    # Short stop at 102 would need high >= 102 + penetration; highs stay at 100.5
    report = simulate([_long_position(), _short_position()], highs, lows)
    assert report.hunt_hit_rate == 0.5


def test_robustness_score_drops_when_drained() -> None:
    # All positions hunted → drain > 0 → robustness < 1.0
    highs = np.full(10, 101.0)
    lows = np.full(10, 97.5)
    report = simulate([_long_position(), _long_position(entry=200.0, stop=195.0)], highs, lows)
    assert all(p.hunted for p in report.positions)
    assert report.robustness_score < 1.0


def test_empty_positions_returns_empty_report() -> None:
    report = simulate([], np.zeros(10), np.zeros(10))
    assert report.positions == []
    assert report.total_pnl_usd == 0.0
    assert report.robustness_score == 1.0
