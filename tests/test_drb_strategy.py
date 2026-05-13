"""Tests for strategies.drb_strategy — Daily Range Breakout."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.drb_strategy import DRBConfig, DRBStrategy


def _bar(
    day: int, *, h: float, low: float, c: float | None = None, o: float | None = None, v: float = 1000.0
) -> BarData:
    """One daily bar on 2026-01-DD."""
    ts = datetime(2026, 1, day, 16, 0, tzinfo=UTC)
    o = o if o is not None else (h + low) / 2
    c = c if c is not None else (h + low) / 2
    return BarData(
        timestamp=ts,
        symbol="NQ1",
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 31, tzinfo=UTC),
        symbol="NQ1",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _strategy(**overrides) -> DRBStrategy:  # type: ignore[no-untyped-def]
    """DRB with EMA bias off + small ATR period for fast tests."""
    base = {"ema_bias_period": 0, "atr_period": 3}
    base.update(overrides)
    return DRBStrategy(DRBConfig(**base))


# ---------------------------------------------------------------------------
# Lookback handling
# ---------------------------------------------------------------------------


def test_first_bar_no_trade_warmup() -> None:
    s = _strategy()
    bar = _bar(1, h=120, low=100, c=110)
    assert s.maybe_enter(bar, [], 10_000.0, _config()) is None


def test_breakout_above_prior_day_high_long() -> None:
    s = _strategy()
    cfg = _config()
    # Build prior-day history
    hist = [
        _bar(1, h=110, low=100),
        _bar(2, h=115, low=105),
        _bar(3, h=120, low=110),  # yesterday's high = 120
    ]
    # Today: breaks above 120
    bar = _bar(4, h=125, low=118, c=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert out.regime == "drb_breakout"


def test_breakout_below_prior_day_low_short() -> None:
    s = _strategy()
    cfg = _config()
    hist = [
        _bar(1, h=120, low=110),
        _bar(2, h=115, low=105),
        _bar(3, h=118, low=108),  # yesterday's low = 108
    ]
    bar = _bar(4, h=109, low=100, c=102)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "SELL"


def test_no_breakout_inside_prior_range() -> None:
    s = _strategy()
    cfg = _config()
    hist = [
        _bar(1, h=120, low=100),
        _bar(2, h=120, low=100),
        _bar(3, h=120, low=100),
    ]
    bar = _bar(4, h=115, low=105, c=110)  # entirely inside 100-120
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


# ---------------------------------------------------------------------------
# State + risk
# ---------------------------------------------------------------------------


def test_one_trade_per_day_latch() -> None:
    s = _strategy()
    cfg = _config()
    hist = [_bar(d, h=120, low=100) for d in range(1, 4)]
    out1 = s.maybe_enter(_bar(4, h=125, low=120, c=124), hist, 10_000.0, cfg)
    out2 = s.maybe_enter(_bar(4, h=130, low=125, c=129), hist, 10_000.0, cfg)
    assert out1 is not None
    assert out2 is None


def test_state_resets_next_day() -> None:
    s = _strategy()
    cfg = _config()
    hist = [_bar(d, h=120, low=100) for d in range(1, 4)]
    s.maybe_enter(_bar(4, h=125, low=120, c=124), hist, 10_000.0, cfg)
    # New day — should be able to fire again if breakout valid
    hist2 = hist + [_bar(4, h=125, low=120, c=124)]
    out = s.maybe_enter(_bar(5, h=130, low=128, c=129), hist2, 10_000.0, cfg)
    assert out is not None  # latch reset


def test_lookback_2_uses_max_high_min_low() -> None:
    s = _strategy(lookback_days=2)
    cfg = _config()
    hist = [
        _bar(1, h=130, low=100),  # high contributes
        _bar(2, h=120, low=90),  # low contributes
    ]
    # Bar at 131 > 130 → breakout
    bar = _bar(3, h=131, low=125, c=130)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"


def test_min_range_filter_blocks_narrow() -> None:
    s = _strategy(lookback_days=1, min_range_pts=20.0)
    cfg = _config()
    hist = [_bar(1, h=110, low=100)]  # range = 10
    bar = _bar(2, h=115, low=110, c=113)
    assert s.maybe_enter(bar, hist, 10_000.0, cfg) is None


def test_position_size_scales_with_equity() -> None:
    s = _strategy(atr_stop_mult=1.0)
    cfg = _config()
    hist = [_bar(d, h=120, low=100) for d in range(1, 4)]
    bar = _bar(4, h=125, low=120, c=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    # ATR ≈ 20 (range 100→120); risk=100; qty=100/20=5
    assert out.qty == pytest.approx(5.0, rel=1e-6)


def test_target_distance_uses_rr_multiple() -> None:
    s = _strategy(atr_stop_mult=1.0, rr_target=3.0)
    cfg = _config()
    hist = [_bar(d, h=120, low=100) for d in range(1, 4)]
    bar = _bar(4, h=125, low=120, c=124)
    out = s.maybe_enter(bar, hist, 10_000.0, cfg)
    assert out is not None
    # entry 124, stop_dist = 20, target = 124 + 60 = 184
    assert out.target == pytest.approx(184.0, rel=1e-3)
    assert out.stop == pytest.approx(104.0, rel=1e-3)
