"""Tests for eta_engine.brain.htf_engine -- higher-timeframe engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.brain.htf_engine import (
    HtfBias,
    HtfEngine,
    classify_structure,
    compute_ema,
    ema_from_bars,
    ema_slope_label,
    swing_highs,
    swing_lows,
)
from eta_engine.core.data_pipeline import BarData

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(
    i: int, open_p: float, high: float, low: float, close: float, vol: float = 1000.0, symbol: str = "MNQ"
) -> BarData:
    return BarData(
        timestamp=_T0 + timedelta(days=i),
        symbol=symbol,
        open=open_p,
        high=high,
        low=low,
        close=close,
        volume=vol,
    )


def _rising_bars(n: int = 60, base: float = 20_000.0, step: float = 10.0) -> list[BarData]:
    """Monotonic rising HH/HL pattern."""
    out = []
    for i in range(n):
        o = base + i * step
        c = o + step * 0.5
        h = c + 2.0
        low = o - 1.0
        out.append(_bar(i, o, h, low, c))
    return out


def _falling_bars(n: int = 60, base: float = 21_000.0, step: float = 10.0) -> list[BarData]:
    out = []
    for i in range(n):
        o = base - i * step
        c = o - step * 0.5
        h = o + 1.0
        low = c - 2.0
        out.append(_bar(i, o, h, low, c))
    return out


def _flat_bars(n: int = 60, level: float = 20_000.0) -> list[BarData]:
    return [_bar(i, level, level + 1.0, level - 1.0, level) for i in range(n)]


def _staircase_bull_bars(n_cycles: int = 4, base: float = 20_000.0, amp: float = 30.0, leg: int = 4) -> list[BarData]:
    """Bull staircase: rise ``leg`` bars, pull back ``leg`` bars, but the
    pullback low is higher than the previous pullback low, and each peak is
    higher than the last. Produces real HH + HL swing pivots.
    """
    bars: list[BarData] = []
    price = base
    idx = 0
    for _cycle in range(n_cycles):
        # Rising leg
        for _ in range(leg):
            nxt = price + amp / leg
            bars.append(_bar(idx, price, nxt + 1.0, price - 1.0, nxt))
            price = nxt
            idx += 1
        # Pullback -- shallower than the rise, so HL > prior HL
        pullback = amp * 0.5
        for _ in range(leg):
            nxt = price - pullback / leg
            bars.append(_bar(idx, price, price + 1.0, nxt - 1.0, nxt))
            price = nxt
            idx += 1
        # Each cycle starts higher than the last
    return bars


def _staircase_bear_bars(n_cycles: int = 4, base: float = 20_800.0, amp: float = 30.0, leg: int = 4) -> list[BarData]:
    """Bear staircase: lower highs + lower lows."""
    bars: list[BarData] = []
    price = base
    idx = 0
    for _ in range(n_cycles):
        # Falling leg
        for _ in range(leg):
            nxt = price - amp / leg
            bars.append(_bar(idx, price, price + 1.0, nxt - 1.0, nxt))
            price = nxt
            idx += 1
        # Bounce
        bounce = amp * 0.5
        for _ in range(leg):
            nxt = price + bounce / leg
            bars.append(_bar(idx, price, nxt + 1.0, price - 1.0, nxt))
            price = nxt
            idx += 1
    return bars


# --------------------------------------------------------------------------- #
# compute_ema / ema_from_bars
# --------------------------------------------------------------------------- #


def test_compute_ema_returns_same_length() -> None:
    vs = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = compute_ema(vs, period=3)
    assert len(out) == len(vs)


def test_compute_ema_is_monotonic_on_rising_input() -> None:
    vs = list(range(1, 21))
    out = compute_ema([float(v) for v in vs], period=5)
    # After seeding, each value should be >= previous
    for a, b in zip(out[5:], out[6:], strict=False):
        assert b >= a


def test_compute_ema_rejects_nonpositive_period() -> None:
    with pytest.raises(ValueError, match="positive"):
        compute_ema([1.0, 2.0], period=0)
    with pytest.raises(ValueError, match="positive"):
        compute_ema([1.0, 2.0], period=-5)


def test_compute_ema_handles_empty_input() -> None:
    assert compute_ema([], period=10) == []


def test_ema_from_bars_uses_close_prices() -> None:
    bars = [_bar(i, 100.0, 101.0, 99.0, 100.0 + i) for i in range(10)]
    out = ema_from_bars(bars, period=3)
    # Last value should be close to most-recent close
    assert out[-1] > 100.0


# --------------------------------------------------------------------------- #
# ema_slope_label
# --------------------------------------------------------------------------- #


def test_ema_slope_label_flat_for_constant_series() -> None:
    assert ema_slope_label([100.0] * 20) == 0.5


def test_ema_slope_label_high_on_strong_rise() -> None:
    vs = [100.0 + i for i in range(20)]
    assert ema_slope_label(vs) == 1.0


def test_ema_slope_label_low_on_strong_fall() -> None:
    vs = [100.0 - i for i in range(20)]
    assert ema_slope_label(vs) == 0.0


def test_ema_slope_label_empty_returns_flat() -> None:
    assert ema_slope_label([]) == 0.5


def test_ema_slope_label_uses_lookback_tail() -> None:
    # Falling for 10 then rising for 10 -> tail should show rise
    vs = [100.0 - i for i in range(10)] + [90.0 + i for i in range(10)]
    tail_score = ema_slope_label(vs, lookback=10)
    full_score = ema_slope_label(vs, lookback=20)
    assert tail_score > full_score


# --------------------------------------------------------------------------- #
# swing_highs / swing_lows
# --------------------------------------------------------------------------- #


def test_swing_highs_finds_local_max() -> None:
    # build a series with a single peak at index 5
    prices = [100, 101, 102, 103, 104, 110, 103, 102, 101, 100]
    bars = [_bar(i, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
    idx = swing_highs(bars, k=2)
    assert 5 in idx


def test_swing_lows_finds_local_min() -> None:
    prices = [110, 108, 107, 106, 105, 100, 105, 106, 107, 108]
    bars = [_bar(i, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
    idx = swing_lows(bars, k=2)
    assert 5 in idx


def test_swing_highs_ignores_flat_plateau() -> None:
    # plateau: no unambiguous swing because strict left-max fails
    bars = [_bar(i, 100, 100, 100, 100) for i in range(11)]
    assert swing_highs(bars, k=2) == []
    assert swing_lows(bars, k=2) == []


def test_swing_highs_excludes_endpoints() -> None:
    # Peak at index 0: never reported because it has no left context
    prices = [200, 100, 100, 100, 100]
    bars = [_bar(i, p, p + 1, p - 1, p) for i, p in enumerate(prices)]
    idx = swing_highs(bars, k=2)
    assert 0 not in idx


# --------------------------------------------------------------------------- #
# classify_structure
# --------------------------------------------------------------------------- #


def test_classify_structure_identifies_higherhigh_higherlow_bull_leg() -> None:
    # Two rising swing highs + two rising swing lows
    # pattern: up down up down up down up (7 bars), HH at 2/4, HL at 3/5
    prices_h = [100, 105, 110, 104, 115, 108, 120]
    prices_l = [99, 102, 108, 103, 114, 107, 119]
    bars = [
        _bar(i, (prices_h[i] + prices_l[i]) / 2, prices_h[i], prices_l[i], (prices_h[i] + prices_l[i]) / 2)
        for i in range(len(prices_h))
    ]
    # Extend so swings are confirmed on both sides
    bars.append(_bar(7, 115, 116, 112, 115))
    bars.append(_bar(8, 114, 115, 111, 114))
    assert classify_structure(bars, k=1) == "HH_HL"


def test_classify_structure_identifies_lowerhigh_lowerlow_bear_leg() -> None:
    prices_h = [120, 118, 115, 117, 110, 114, 105]
    prices_l = [110, 112, 108, 110, 103, 107, 100]
    bars = [
        _bar(i, (prices_h[i] + prices_l[i]) / 2, prices_h[i], prices_l[i], (prices_h[i] + prices_l[i]) / 2)
        for i in range(len(prices_h))
    ]
    bars.append(_bar(7, 108, 109, 104, 108))
    bars.append(_bar(8, 109, 110, 105, 109))
    assert classify_structure(bars, k=1) == "LH_LL"


def test_classify_structure_returns_neutral_when_swings_disagree() -> None:
    # Structure where HH is present but LL is not (chopiness)
    prices = [100, 105, 100, 106, 100, 107, 100, 108, 100]
    bars = [_bar(i, p, p + 2, p - 2, p) for i, p in enumerate(prices)]
    assert classify_structure(bars, k=1) == "NEUTRAL"


def test_classify_structure_returns_neutral_on_too_few_bars() -> None:
    assert classify_structure([], k=2) == "NEUTRAL"
    short = [_bar(i, 100, 101, 99, 100) for i in range(3)]
    assert classify_structure(short, k=2) == "NEUTRAL"


# --------------------------------------------------------------------------- #
# HtfEngine.top_down
# --------------------------------------------------------------------------- #


def test_htf_engine_bullish_when_daily_rising_and_h4_higher_highs() -> None:
    eng = HtfEngine(daily_ema_period=20, slope_lookback=10)
    # step=200 gives ~1% moves per bar, so the EMA tail easily exceeds the 2%
    # threshold that maps to slope score 1.0.
    daily = _rising_bars(n=40, step=200.0)
    # Staircase fixture produces real HH + HL pivots (monotonic rises do not).
    h4 = _staircase_bull_bars(n_cycles=4, base=20_200.0, amp=30.0, leg=4)
    out = eng.top_down(daily, h4)
    assert out.bias == +1
    assert out.agreement is True
    assert out.daily_ema_slope > 0.9
    assert out.h4_struct == "HH_HL"


def test_htf_engine_bearish_when_daily_falling_and_h4_lower_lows() -> None:
    eng = HtfEngine(daily_ema_period=20, slope_lookback=10)
    daily = _falling_bars(n=40, step=200.0)
    h4 = _staircase_bear_bars(n_cycles=4, base=20_800.0, amp=30.0, leg=4)
    out = eng.top_down(daily, h4)
    assert out.bias == -1
    assert out.agreement is True
    assert out.h4_struct == "LH_LL"


def test_htf_engine_neutral_when_4h_disagrees_with_daily() -> None:
    eng = HtfEngine(daily_ema_period=20, slope_lookback=10)
    daily = _rising_bars(n=40, step=200.0)  # daily bullish
    # Real LH_LL pivots required for 4H to disagree; monotonic falls are NEUTRAL.
    h4 = _staircase_bear_bars(n_cycles=4, base=20_800.0, amp=30.0, leg=4)
    out = eng.top_down(daily, h4)
    assert out.bias == 0
    assert out.agreement is False


def test_htf_engine_keeps_bias_when_4h_is_neutral() -> None:
    eng = HtfEngine(daily_ema_period=20, slope_lookback=10)
    daily = _rising_bars(n=40)
    h4 = _flat_bars(n=30)
    out = eng.top_down(daily, h4)
    assert out.bias == +1
    # agreement=False because 4H didn't confirm, just didn't disagree
    assert out.agreement is False


def test_htf_engine_neutral_when_not_enough_daily_bars() -> None:
    eng = HtfEngine(daily_ema_period=50)
    daily = _rising_bars(n=10)  # fewer than period
    h4 = _rising_bars(n=30)
    out = eng.top_down(daily, h4)
    assert out.bias == 0
    assert out.daily_ema == []


def test_htf_engine_context_for_trend_bias_shape() -> None:
    eng = HtfEngine(daily_ema_period=10, slope_lookback=5)
    ctx = eng.context_for_trend_bias(_rising_bars(n=20), _rising_bars(n=15))
    assert set(ctx.keys()) == {"daily_ema", "h4_struct", "bias"}
    assert isinstance(ctx["daily_ema"], list)
    assert ctx["h4_struct"] in {"HH_HL", "LH_LL", "NEUTRAL"}
    assert ctx["bias"] in {-1, 0, 1}


def test_htf_engine_rejects_bad_params() -> None:
    with pytest.raises(ValueError, match="daily_ema_period"):
        HtfEngine(daily_ema_period=0)
    with pytest.raises(ValueError, match="swing_k"):
        HtfEngine(daily_swing_k=0)
    with pytest.raises(ValueError, match="swing_k"):
        HtfEngine(h4_swing_k=-1)


# --------------------------------------------------------------------------- #
# Integration: HtfEngine output feeds TrendBiasFeature
# --------------------------------------------------------------------------- #


def test_htf_output_feeds_trend_bias_feature() -> None:
    """The whole point of the engine: feed features.trend_bias without adapter."""
    from eta_engine.features.trend_bias import TrendBiasFeature

    eng = HtfEngine(daily_ema_period=20, slope_lookback=10)
    daily = _rising_bars(n=40)
    h4 = _rising_bars(n=30, base=20_200.0)
    ctx = eng.context_for_trend_bias(daily, h4)

    # Use a recent daily bar as the reference
    bar = daily[-1]
    feat = TrendBiasFeature()
    res = feat.evaluate(bar, ctx)
    # Bull context + bull bias => trend_bias score should be >= 0.5
    assert res.normalized_score >= 0.5


def test_htf_bias_model_round_trip() -> None:
    b = HtfBias(
        daily_ema=[1.0, 2.0, 3.0],
        daily_ema_slope=0.9,
        daily_struct="HH_HL",
        h4_struct="HH_HL",
        bias=+1,
        agreement=True,
    )
    # Pydantic serializes cleanly
    d = b.model_dump()
    assert d["bias"] == 1
    b2 = HtfBias(**d)
    assert b2.agreement is True
