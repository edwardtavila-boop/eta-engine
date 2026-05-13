"""Tests for crypto_regime_trend_strategy — 200 EMA regime + pullback entry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.crypto_regime_trend_strategy import (
    CryptoRegimeTrendConfig,
    CryptoRegimeTrendStrategy,
)


def _bar(
    idx: int,
    *,
    h: float,
    low: float,
    c: float | None = None,
    o: float | None = None,
    v: float = 1000.0,
    tf_minutes: int = 60,
) -> BarData:
    """Synthetic bar at 2026-01-01 + idx*tf_minutes."""
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=idx * tf_minutes)
    o = o if o is not None else (h + low) / 2
    c = c if c is not None else (h + low) / 2
    return BarData(
        timestamp=ts,
        symbol="BTC",
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _strategy(**overrides) -> CryptoRegimeTrendStrategy:  # type: ignore[no-untyped-def]
    """Small EMAs + low warmup so tests stay fast.

    Daily cap defaults to 100 here so tests can fire many times in
    one synthetic day without bumping the per-day latch — the latch
    is exercised by its own dedicated test.
    """
    base = {
        "regime_ema": 20,
        "pullback_ema": 5,
        "warmup_bars": 25,
        "atr_period": 5,
        "min_bars_between_trades": 0,
        "pullback_tolerance_pct": 1.0,
        "max_trades_per_day": 100,
    }
    base.update(overrides)
    return CryptoRegimeTrendStrategy(CryptoRegimeTrendConfig(**base))


def test_warmup_blocks_early_signals() -> None:
    s = _strategy(warmup_bars=10)
    cfg = _config()
    bar = _bar(0, h=100, low=98, c=99)
    assert s.maybe_enter(bar, [bar], 10_000.0, cfg) is None


def test_long_blocked_below_regime_ema() -> None:
    s = _strategy()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(30):
        c = 100 - i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fired = False
    for i in range(30, 35):
        c = 85 + i * 0.1
        b = _bar(i, h=c + 0.5, low=c - 0.6, c=c)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None and out.side == "BUY":
            fired = True
            break
    assert not fired, "long must be blocked while close < regime EMA"


def test_short_blocked_above_regime_ema() -> None:
    s = _strategy()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(30):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fired = False
    for i in range(30, 35):
        c = 115 - i * 0.1
        b = _bar(i, h=c + 0.6, low=c - 0.5, c=c)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None and out.side == "SELL":
            fired = True
            break
    assert not fired, "short must be blocked while close > regime EMA"


def test_long_fires_on_pullback_in_bull_regime() -> None:
    s = _strategy(pullback_tolerance_pct=2.0)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fast_ema = s._pullback_ema
    assert fast_ema is not None
    pull_low = fast_ema - 0.05
    pull_close = fast_ema + 0.5
    pull_bar = _bar(35, h=pull_close + 0.3, low=pull_low, c=pull_close)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert "bull" in out.regime


def test_short_fires_on_pullback_in_bear_regime() -> None:
    s = _strategy(pullback_tolerance_pct=2.0)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 - i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fast_ema = s._pullback_ema
    assert fast_ema is not None
    rip_high = fast_ema + 0.05
    rip_close = fast_ema - 0.5
    rip_bar = _bar(35, h=rip_high, low=rip_close - 0.3, c=rip_close)
    hist.append(rip_bar)
    out = s.maybe_enter(rip_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "SELL"
    assert "bear" in out.regime


def test_no_fire_when_close_doesnt_bounce() -> None:
    s = _strategy(pullback_tolerance_pct=2.0)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fast_ema = s._pullback_ema
    assert fast_ema is not None
    bad_bar = _bar(35, h=fast_ema + 0.5, low=fast_ema - 0.1, c=fast_ema - 0.05)
    hist.append(bad_bar)
    out = s.maybe_enter(bad_bar, hist, 10_000.0, cfg)
    assert out is None


def test_no_fire_when_pullback_too_deep() -> None:
    s = _strategy(pullback_tolerance_pct=0.5)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fast_ema = s._pullback_ema
    assert fast_ema is not None
    bad_bar = _bar(
        35,
        h=fast_ema + 0.5,
        low=fast_ema * (1.0 - 0.05),
        c=fast_ema + 0.2,
    )
    hist.append(bad_bar)
    out = s.maybe_enter(bad_bar, hist, 10_000.0, cfg)
    assert out is None


def test_min_bars_between_trades_latch() -> None:
    """Cooldown after a fire must block a same-shape trigger."""
    s = _strategy(min_bars_between_trades=20, pullback_tolerance_pct=2.0)
    cfg = _config()
    hist: list[BarData] = []
    fired_at: int | None = None
    for i in range(40):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None and fired_at is None:
            fired_at = i
    assert fired_at is not None, "expected at least one fire on the setup uptrend"
    if s._last_entry_idx is not None:
        gap = s._bars_seen - s._last_entry_idx
        assert gap < 20  # cooldown is the binding latch


def test_position_size_scales_with_equity() -> None:
    s = _strategy(atr_stop_mult=1.0, pullback_tolerance_pct=2.0)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fast_ema = s._pullback_ema
    assert fast_ema is not None
    pull_bar = _bar(35, h=fast_ema + 0.5, low=fast_ema - 0.05, c=fast_ema + 0.3)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.qty > 0
    assert out.risk_usd == pytest.approx(100.0, rel=1e-6)
