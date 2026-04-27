"""
EVOLUTIONARY TRADING ALGO  //  tests.test_risk_engine
=========================================
Risk engine: leverage, sizing, Kelly, circuit breakers.
"""

from __future__ import annotations

import pytest

from eta_engine.core.risk_engine import (
    calculate_max_leverage,
    check_daily_loss_cap,
    check_max_drawdown_kill,
    dynamic_position_size,
    fractional_kelly,
    liquidation_distance,
)

# ---------------------------------------------------------------------------
# Leverage ceiling
# ---------------------------------------------------------------------------


class TestCalculateMaxLeverage:
    def test_normal_conditions(self) -> None:
        lev = calculate_max_leverage(price=21550.0, atr_14_5m=18.5)
        assert 5.0 <= lev <= 500.0

    def test_higher_atr_lowers_leverage(self) -> None:
        low_vol = calculate_max_leverage(price=21550.0, atr_14_5m=10.0)
        high_vol = calculate_max_leverage(price=21550.0, atr_14_5m=30.0)
        assert low_vol > high_vol

    def test_zero_atr_raises(self) -> None:
        with pytest.raises(ValueError, match="ATR must be positive"):
            calculate_max_leverage(price=21550.0, atr_14_5m=0.0)

    def test_extreme_vol_raises(self) -> None:
        with pytest.raises(ValueError, match="below safety floor"):
            calculate_max_leverage(price=21550.0, atr_14_5m=2000.0)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class TestDynamicPositionSize:
    @pytest.mark.parametrize(
        "equity,risk_pct,atr,price,expected_min",
        [
            (50_000, 0.01, 18.5, 21550.0, 1000.0),
            (100_000, 0.02, 18.5, 21550.0, 5000.0),
            (10_000, 0.005, 10.0, 21550.0, 100.0),
        ],
    )
    def test_sizing_scales_with_equity(
        self,
        equity: float,
        risk_pct: float,
        atr: float,
        price: float,
        expected_min: float,
    ) -> None:
        size = dynamic_position_size(equity, risk_pct, atr, price)
        assert size > expected_min

    def test_risk_cap_enforced(self) -> None:
        with pytest.raises(ValueError, match="exceeds 10% hard cap"):
            dynamic_position_size(50_000, 0.15, 18.5, 21550.0)

    def test_zero_equity_raises(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            dynamic_position_size(0, 0.01, 18.5, 21550.0)


# ---------------------------------------------------------------------------
# Kelly criterion
# ---------------------------------------------------------------------------


class TestFractionalKelly:
    def test_positive_edge(self) -> None:
        kelly = fractional_kelly(win_rate=0.55, avg_win_r=1.5, avg_loss_r=1.0)
        assert kelly > 0.0

    def test_no_edge_returns_zero(self) -> None:
        kelly = fractional_kelly(win_rate=0.30, avg_win_r=1.0, avg_loss_r=1.0)
        assert kelly == 0.0

    @pytest.mark.parametrize("fraction", [0.1, 0.25, 0.5, 1.0])
    def test_fraction_scales(self, fraction: float) -> None:
        k = fractional_kelly(0.55, 1.5, 1.0, fraction=fraction)
        assert k >= 0.0


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------


class TestDailyLossCap:
    def test_under_cap(self) -> None:
        assert check_daily_loss_cap(-500.0, 0.025, 50_000.0) is False

    def test_at_cap(self) -> None:
        assert check_daily_loss_cap(-1250.0, 0.025, 50_000.0) is True

    def test_over_cap(self) -> None:
        assert check_daily_loss_cap(-2000.0, 0.025, 50_000.0) is True


class TestMaxDrawdownKill:
    def test_no_drawdown(self) -> None:
        assert check_max_drawdown_kill(50_000.0, 50_000.0, 0.08) is False

    def test_at_kill_threshold(self) -> None:
        assert check_max_drawdown_kill(50_000.0, 46_000.0, 0.08) is True

    def test_above_kill(self) -> None:
        assert check_max_drawdown_kill(50_000.0, 40_000.0, 0.08) is True


# ---------------------------------------------------------------------------
# Liquidation
# ---------------------------------------------------------------------------


class TestLiquidationDistance:
    def test_isolated_basic(self) -> None:
        dist = liquidation_distance(21550.0, 20.0, "isolated")
        assert dist > 0

    def test_cross_is_tighter(self) -> None:
        iso = liquidation_distance(21550.0, 20.0, "isolated")
        cross = liquidation_distance(21550.0, 20.0, "cross")
        assert cross < iso  # cross margin is more aggressive
