"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_models.

Unit tests for :mod:`eta_engine.strategies.models` -- the shared
value objects for the strategies package.
"""

from __future__ import annotations

import pytest

from eta_engine.strategies.models import (
    FLAT_SIGNAL,
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)

# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------


class TestBar:
    def test_bar_is_immutable(self) -> None:
        bar = Bar(ts=1, open=100.0, high=110.0, low=95.0, close=105.0)
        with pytest.raises(AttributeError):
            bar.close = 999.0  # type: ignore[misc]

    def test_body_is_absolute(self) -> None:
        up = Bar(ts=1, open=100.0, high=110.0, low=95.0, close=105.0)
        down = Bar(ts=2, open=105.0, high=110.0, low=95.0, close=100.0)
        assert up.body == pytest.approx(5.0)
        assert down.body == pytest.approx(5.0)

    def test_range_is_high_minus_low(self) -> None:
        bar = Bar(ts=1, open=100.0, high=110.0, low=95.0, close=105.0)
        assert bar.range == pytest.approx(15.0)

    def test_is_bull_and_is_bear_are_mutually_exclusive(self) -> None:
        bull = Bar(ts=1, open=100.0, high=110.0, low=95.0, close=105.0)
        bear = Bar(ts=2, open=105.0, high=110.0, low=95.0, close=100.0)
        doji = Bar(ts=3, open=100.0, high=110.0, low=95.0, close=100.0)
        assert bull.is_bull and not bull.is_bear
        assert bear.is_bear and not bear.is_bull
        assert not doji.is_bull and not doji.is_bear

    def test_volume_defaults_to_zero(self) -> None:
        bar = Bar(ts=1, open=100.0, high=110.0, low=95.0, close=105.0)
        assert bar.volume == 0.0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestSide:
    def test_side_values_are_strings(self) -> None:
        assert Side.LONG.value == "LONG"
        assert Side.SHORT.value == "SHORT"
        assert Side.FLAT.value == "FLAT"

    def test_side_identity(self) -> None:
        a = Side.LONG
        b = Side.LONG
        assert a is b


class TestStrategyId:
    def test_all_six_strategies_present(self) -> None:
        expected = {
            "liquidity_sweep_displacement",
            "ob_breaker_retest",
            "fvg_fill_confluence",
            "mtf_trend_following",
            "regime_adaptive_allocation",
            "rl_full_automation",
        }
        assert {s.value for s in StrategyId} == expected


# ---------------------------------------------------------------------------
# StrategySignal
# ---------------------------------------------------------------------------


class TestStrategySignal:
    def test_flat_signal_not_actionable(self) -> None:
        assert FLAT_SIGNAL.is_actionable is False

    def test_actionable_requires_side_confidence_and_risk(self) -> None:
        good = StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            confidence=5.0,
            risk_mult=1.0,
        )
        no_conf = StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            confidence=0.0,
            risk_mult=1.0,
        )
        no_risk = StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            confidence=5.0,
            risk_mult=0.0,
        )
        flat = StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.FLAT,
            confidence=5.0,
            risk_mult=1.0,
        )
        assert good.is_actionable
        assert not no_conf.is_actionable
        assert not no_risk.is_actionable
        assert not flat.is_actionable

    def test_rr_standard(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            entry=100.0,
            stop=95.0,
            target=115.0,
        )
        assert sig.rr == pytest.approx(3.0)

    def test_rr_zero_when_stop_equals_entry(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
            entry=100.0,
            stop=100.0,
            target=105.0,
        )
        assert sig.rr == 0.0

    def test_as_dict_is_json_safe(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.FVG_FILL_CONFLUENCE,
            side=Side.SHORT,
            entry=100.0,
            stop=105.0,
            target=90.0,
            confidence=7.5,
            risk_mult=0.9,
            rationale_tags=("fvg_fill", "rr=1.5"),
            meta={"fvg_low": 99.0},
        )
        d = sig.as_dict()
        assert d["strategy"] == "fvg_fill_confluence"
        assert d["side"] == "SHORT"
        assert d["rr"] == pytest.approx(2.0)
        assert d["rationale_tags"] == ["fvg_fill", "rr=1.5"]
        assert d["meta"] == {"fvg_low": 99.0}

    def test_signal_is_immutable(self) -> None:
        sig = StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.LONG,
        )
        with pytest.raises(AttributeError):
            sig.confidence = 9.0  # type: ignore[misc]
