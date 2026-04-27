"""EVOLUTIONARY TRADING ALGO  //  tests.test_strategies_smc_primitives.

Unit tests for pure SMC/ICT detectors. No fixtures -- every test
constructs its own bar stream to keep failures easy to read.
"""

from __future__ import annotations

import pytest

from eta_engine.strategies.models import Bar, Side
from eta_engine.strategies.smc_primitives import (
    SweepSide,
    above_moving_average,
    detect_break_of_structure,
    detect_displacement,
    detect_fvg,
    detect_liquidity_sweep,
    detect_order_block,
    find_equal_levels,
    simple_ma,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(
    ts: int,
    o: float,
    h: float,
    low: float,
    c: float,
    v: float = 1000.0,
) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=low, close=c, volume=v)


def _steady_uptrend(n: int = 50, start: float = 100.0, step: float = 1.0) -> list[Bar]:
    """Clean uptrend where every close > open and close = prev_close + step."""
    bars: list[Bar] = []
    close = start
    for i in range(n):
        o = close
        close = close + step
        h = close + 0.2
        lo = o - 0.2
        bars.append(_bar(i, o, h, lo, close))
    return bars


# ---------------------------------------------------------------------------
# find_equal_levels
# ---------------------------------------------------------------------------


class TestFindEqualLevels:
    def test_returns_empty_when_short(self) -> None:
        bars = [_bar(0, 100.0, 101.0, 99.0, 100.5)]
        assert find_equal_levels(bars, min_count=2) == []

    def test_finds_equal_highs(self) -> None:
        # Build 40 bars with two equal-highs at 110.0
        bars: list[Bar] = []
        for i in range(40):
            h = 110.0 if i in (10, 20) else 105.0
            bars.append(_bar(i, 100.0, h, 99.0, 102.0))
        levels = find_equal_levels(bars, tolerance_pct=0.0005, min_count=2)
        assert any(level.side is SweepSide.HIGH and level.level == pytest.approx(110.0) for level in levels)

    def test_respects_tolerance(self) -> None:
        bars: list[Bar] = []
        for i in range(40):
            h = 110.0 if i == 10 else (110.5 if i == 20 else 105.0)
            bars.append(_bar(i, 100.0, h, 99.0, 102.0))
        # tolerance 0.0001 (1 bp) -- 110 and 110.5 are NOT equal
        levels = find_equal_levels(bars, tolerance_pct=0.0001, min_count=2)
        for level in levels:
            if level.side is SweepSide.HIGH:
                assert level.level != pytest.approx(110.0)


# ---------------------------------------------------------------------------
# detect_liquidity_sweep
# ---------------------------------------------------------------------------


class TestDetectLiquiditySweep:
    def test_returns_none_on_short_stream(self) -> None:
        assert detect_liquidity_sweep([_bar(0, 100.0, 101.0, 99.0, 100.0)]) is None

    def test_detects_bullish_sweep(self) -> None:
        # 40 bars with equal-lows at 100.0; then last bar wicks below and closes back
        bars: list[Bar] = []
        for i in range(40):
            lo = 100.0 if i in (10, 20) else 101.0
            bars.append(_bar(i, 102.0, 103.0, lo, 102.5))
        # Final sweep bar: wicks to 99.5, closes at 101.0 (above 100.0)
        bars.append(_bar(40, 101.0, 102.0, 99.5, 101.0))
        sweep = detect_liquidity_sweep(bars, min_depth_pct=0.001)
        assert sweep is not None
        assert sweep.side is SweepSide.LOW
        assert sweep.level == pytest.approx(100.0)
        assert sweep.close_back == pytest.approx(101.0)
        assert sweep.depth_pct > 0.001

    def test_detects_bearish_sweep(self) -> None:
        bars: list[Bar] = []
        for i in range(40):
            h = 110.0 if i in (10, 20) else 109.0
            bars.append(_bar(i, 107.0, h, 106.0, 108.0))
        # Final: wick to 110.5, close back below at 109.5
        bars.append(_bar(40, 109.0, 110.5, 108.5, 109.5))
        sweep = detect_liquidity_sweep(bars, min_depth_pct=0.001)
        assert sweep is not None
        assert sweep.side is SweepSide.HIGH

    def test_no_sweep_when_no_close_back(self) -> None:
        bars: list[Bar] = []
        for i in range(40):
            lo = 100.0 if i in (10, 20) else 101.0
            bars.append(_bar(i, 102.0, 103.0, lo, 102.5))
        # Close remains below 100 -- not a sweep, a break
        bars.append(_bar(40, 101.0, 101.5, 99.0, 99.5))
        assert detect_liquidity_sweep(bars, min_depth_pct=0.001) is None


# ---------------------------------------------------------------------------
# detect_displacement
# ---------------------------------------------------------------------------


class TestDetectDisplacement:
    def test_returns_none_on_short_stream(self) -> None:
        bars = _steady_uptrend(n=10)
        assert detect_displacement(bars, lookback=20) is None

    def test_detects_bullish_displacement(self) -> None:
        # 20 small bars (body=1.0) then a huge body
        bars: list[Bar] = []
        for i in range(20):
            bars.append(_bar(i, 100.0, 102.0, 99.5, 101.0))
        bars.append(_bar(20, 101.0, 108.0, 100.5, 107.5))  # body=6.5
        disp = detect_displacement(bars, lookback=20, body_mult=1.8)
        assert disp is not None
        assert disp.direction is Side.LONG
        assert disp.body_mult >= 1.8

    def test_detects_bearish_displacement(self) -> None:
        bars: list[Bar] = []
        for i in range(20):
            bars.append(_bar(i, 100.0, 102.0, 99.5, 101.0))
        bars.append(_bar(20, 101.0, 101.5, 94.0, 94.5))  # big red
        disp = detect_displacement(bars, lookback=20, body_mult=1.8)
        assert disp is not None
        assert disp.direction is Side.SHORT

    def test_rejects_below_threshold(self) -> None:
        bars: list[Bar] = []
        for i in range(20):
            bars.append(_bar(i, 100.0, 102.0, 99.5, 101.0))
        # last bar body ~= median (1.0)
        bars.append(_bar(20, 101.0, 102.5, 100.0, 102.0))
        assert detect_displacement(bars, body_mult=1.8) is None


# ---------------------------------------------------------------------------
# detect_fvg
# ---------------------------------------------------------------------------


class TestDetectFvg:
    def test_returns_none_on_short_stream(self) -> None:
        bars = [_bar(0, 100.0, 101.0, 99.0, 100.5)]
        assert detect_fvg(bars) is None

    def test_detects_bullish_fvg(self) -> None:
        # bar[0] high=101, bar[1] unrelated, bar[2] low=103 => bullish gap
        bars = [
            _bar(0, 100.0, 101.0, 99.0, 100.5),
            _bar(1, 101.0, 103.5, 100.5, 103.0),
            _bar(2, 103.0, 105.0, 103.0, 104.5),
        ]
        fvg = detect_fvg(bars)
        assert fvg is not None
        assert fvg.direction is Side.LONG
        assert fvg.low == pytest.approx(101.0)
        assert fvg.high == pytest.approx(103.0)

    def test_detects_bearish_fvg(self) -> None:
        # bar[0] low=105, bar[2] high=103 => bearish gap
        bars = [
            _bar(0, 106.0, 107.0, 105.0, 106.5),
            _bar(1, 106.0, 106.0, 103.5, 104.0),
            _bar(2, 104.0, 103.0, 101.0, 101.5),
        ]
        fvg = detect_fvg(bars)
        assert fvg is not None
        assert fvg.direction is Side.SHORT

    def test_marks_filled_fvg_none(self) -> None:
        # Bullish FVG then price dips back into the zone
        bars = [
            _bar(0, 100.0, 101.0, 99.0, 100.5),
            _bar(1, 101.0, 103.5, 100.5, 103.0),
            _bar(2, 103.0, 105.0, 103.0, 104.5),
            _bar(3, 104.5, 105.5, 100.5, 101.5),  # dips into gap
        ]
        assert detect_fvg(bars) is None


# ---------------------------------------------------------------------------
# detect_break_of_structure
# ---------------------------------------------------------------------------


class TestDetectBreakOfStructure:
    def test_returns_none_on_short_stream(self) -> None:
        bars = _steady_uptrend(n=5)
        assert detect_break_of_structure(bars, window=3) is None

    def test_detects_bullish_bos(self) -> None:
        # Simple stepped pattern: up to 110 (swing high at idx 4),
        # retrace down, then close breaks above 110
        closes = [100.0, 102.0, 104.0, 107.0, 110.0, 108.0, 106.0, 109.0, 111.5]
        bars: list[Bar] = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i > 0 else c - 1
            h = c + 0.5
            lo = min(o, c) - 0.5
            bars.append(_bar(i, o, h, lo, c))
        bos = detect_break_of_structure(bars, window=2)
        assert bos is not None
        assert bos.direction is Side.LONG
        assert bos.break_bar_index == len(bars) - 1

    def test_detects_bearish_bos(self) -> None:
        closes = [110.0, 108.0, 106.0, 103.0, 100.0, 102.0, 104.0, 101.0, 98.5]
        bars: list[Bar] = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i > 0 else c + 1
            h = max(o, c) + 0.5
            lo = c - 0.5
            bars.append(_bar(i, o, h, lo, c))
        bos = detect_break_of_structure(bars, window=2)
        assert bos is not None
        assert bos.direction is Side.SHORT


# ---------------------------------------------------------------------------
# detect_order_block
# ---------------------------------------------------------------------------


class TestDetectOrderBlock:
    def test_finds_last_opposing_candle_before_bullish_bos(self) -> None:
        closes = [100.0, 102.0, 104.0, 107.0, 110.0, 108.0, 106.0, 109.0, 111.5]
        opens = [99.0, 100.0, 102.0, 104.0, 107.0, 110.0, 108.0, 106.0, 109.0]
        bars: list[Bar] = []
        for i, (o, c) in enumerate(zip(opens, closes, strict=True)):
            h = max(o, c) + 0.5
            lo = min(o, c) - 0.5
            bars.append(_bar(i, o, h, lo, c))
        bos = detect_break_of_structure(bars, window=2)
        assert bos is not None
        ob = detect_order_block(bars, bos)
        assert ob is not None
        assert ob.direction is Side.LONG
        # Must be a bearish candle (close < open) before the pivot
        assert bars[ob.bar_index].is_bear or bars[ob.bar_index].close == bars[ob.bar_index].open

    def test_returns_none_if_no_opposing_candle(self) -> None:
        # All bull candles -> no bearish OB exists before the bullish BOS
        bars = _steady_uptrend(n=20, start=100.0, step=1.0)
        # Construct a trivial bullish BOS by hand
        from eta_engine.strategies.smc_primitives import BreakOfStructure

        bos = BreakOfStructure(
            direction=Side.LONG,
            pivot_price=110.0,
            pivot_bar_index=10,
            break_bar_index=19,
        )
        assert detect_order_block(bars, bos) is None


# ---------------------------------------------------------------------------
# moving average filter
# ---------------------------------------------------------------------------


class TestMovingAverage:
    def test_simple_ma_returns_zero_when_short(self) -> None:
        bars = _steady_uptrend(n=5)
        assert simple_ma(bars, period=200) == 0.0

    def test_simple_ma_matches_arithmetic_mean(self) -> None:
        bars = _steady_uptrend(n=10, start=100.0, step=1.0)
        expected = sum(b.close for b in bars[-5:]) / 5.0
        assert simple_ma(bars, period=5) == pytest.approx(expected)

    def test_above_ma_returns_long_in_uptrend(self) -> None:
        bars = _steady_uptrend(n=50, start=100.0, step=1.0)
        assert above_moving_average(bars, period=20) is Side.LONG

    def test_above_ma_returns_flat_when_short(self) -> None:
        bars = _steady_uptrend(n=5)
        assert above_moving_average(bars, period=200) is Side.FLAT
