"""Tests for crypto strategy family — trend / mean-rev / scalp.

Each strategy is exercised on a small synthetic bar stream that
makes its trigger inevitable (rising series for trend, oversold dip
for mean-rev, narrow consolidation + breakout for scalp).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.crypto_meanrev_strategy import (
    CryptoMeanRevConfig,
    CryptoMeanRevStrategy,
)
from eta_engine.strategies.crypto_scalp_strategy import (
    CryptoScalpConfig,
    CryptoScalpStrategy,
)
from eta_engine.strategies.crypto_trend_strategy import (
    CryptoTrendConfig,
    CryptoTrendStrategy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    """Synthetic bar at 2026-01-01 00:00 UTC + idx*tf_minutes."""
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


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def _trend(**overrides) -> CryptoTrendStrategy:  # type: ignore[no-untyped-def]
    base = {
        "warmup_bars": 5,
        "fast_ema": 3,
        "slow_ema": 7,
        "htf_ema": 0,
        "atr_period": 5,
        "min_bars_between_trades": 0,
    }
    base.update(overrides)
    return CryptoTrendStrategy(CryptoTrendConfig(**base))


def test_trend_warmup_blocks_early_signals() -> None:
    s = _trend()
    cfg = _config()
    # Single rising bar — too early to sample EMAs
    bar = _bar(0, h=110, low=100, c=105)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_trend_long_fires_on_fast_over_slow_cross() -> None:
    """Build a downtrend (fast<slow) then sharply up: fast crosses above slow."""
    s = _trend()
    cfg = _config()
    hist: list[BarData] = []
    # Downtrend: 100 → 60
    for i in range(15):
        b = _bar(i, h=100 - i * 2 + 1, low=100 - i * 2 - 1, c=100 - i * 2)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Sharp reversal up — fast EMA snaps above slow within a few bars
    out_signal: object = None
    for i in range(15, 35):
        b = _bar(i, h=70 + (i - 14) * 5 + 2, low=70 + (i - 14) * 5 - 2, c=70 + (i - 14) * 5)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            out_signal = out
            break
    assert out_signal is not None
    assert out_signal.side == "BUY"  # type: ignore[attr-defined]
    assert out_signal.regime == "crypto_trend"  # type: ignore[attr-defined]


def test_trend_htf_bias_blocks_long_below_htf() -> None:
    """Even on a fast cross-up, if price < HTF EMA, no long fires."""
    s = _trend(htf_ema=50)
    cfg = _config()
    hist: list[BarData] = []
    # Long warmup at high price — sets HTF high
    for i in range(60):
        b = _bar(i, h=510, low=500, c=505)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Short downtrend then bounce — but bounce is far below HTF (~505)
    for i in range(60, 75):
        b = _bar(i, h=205, low=195, c=200)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fired = False
    for i in range(75, 90):
        b = _bar(i, h=210 + i, low=200 + i, c=205 + i)  # rising but still <<510
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None and out.side == "BUY":
            fired = True
            break
    assert not fired, "long must not fire below HTF EMA"


# ---------------------------------------------------------------------------
# Mean reversion
# ---------------------------------------------------------------------------


def _meanrev(**overrides) -> CryptoMeanRevStrategy:  # type: ignore[no-untyped-def]
    base = {
        "bb_period": 10,
        "rsi_period": 5,
        "atr_period": 5,
        "min_bars_between_trades": 0,
    }
    base.update(overrides)
    return CryptoMeanRevStrategy(CryptoMeanRevConfig(**base))


def test_meanrev_warmup_blocks_early_signals() -> None:
    s = _meanrev()
    cfg = _config()
    bar = _bar(0, h=110, low=90, c=100)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_meanrev_long_on_lower_band_touch_with_oversold_rsi() -> None:
    """Steady high price then a sharp dump → low touches lower band, RSI <30."""
    s = _meanrev()
    cfg = _config()
    hist: list[BarData] = []
    # Stable price 100 to set narrow band
    for i in range(15):
        b = _bar(i, h=101, low=99, c=100)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Sharp dump to 80 — RSI plummets, low pierces lower band
    out_signal = None
    for i in range(15, 25):
        b = _bar(i, h=85, low=79, c=80)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            out_signal = out
            break
    assert out_signal is not None
    assert out_signal.side == "BUY"
    assert out_signal.regime == "crypto_meanrev"


def test_meanrev_short_on_upper_band_touch_with_overbought_rsi() -> None:
    s = _meanrev()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(15):
        b = _bar(i, h=101, low=99, c=100)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    out_signal = None
    for i in range(15, 25):
        b = _bar(i, h=121, low=115, c=120)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            out_signal = out
            break
    assert out_signal is not None
    assert out_signal.side == "SELL"


def test_meanrev_no_signal_on_drift_within_band() -> None:
    """Slow drift inside the band should not fire — only extremes do."""
    s = _meanrev()
    cfg = _config()
    hist: list[BarData] = []
    fired = False
    for i in range(40):
        # Tiny drift; bar high/low never pierces 2σ band, RSI stays 40-60
        c = 100 + (i % 2) * 0.1
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            fired = True
            break
    assert not fired


# ---------------------------------------------------------------------------
# Scalper
# ---------------------------------------------------------------------------


def _scalp(**overrides) -> CryptoScalpStrategy:  # type: ignore[no-untyped-def]
    base = {
        "lookback_bars": 5,
        "vwap_lookback": 5,
        "rsi_period": 5,
        "atr_period": 5,
        "min_bars_between_trades": 0,
    }
    base.update(overrides)
    return CryptoScalpStrategy(CryptoScalpConfig(**base))


def test_scalp_warmup_blocks_early_signals() -> None:
    s = _scalp()
    cfg = _config()
    bar = _bar(0, h=110, low=100, c=105)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_scalp_long_fires_on_break_with_vwap_and_rsi_alignment() -> None:
    s = _scalp()
    cfg = _config()
    hist: list[BarData] = []
    # Consolidation 100-105 with rising drift to push RSI > 50
    for i in range(8):
        c = 100 + i * 0.3
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c, v=1000.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Breakout bar above the 5-bar high
    out_signal = None
    for i in range(8, 14):
        c = 110 + (i - 7) * 2
        b = _bar(i, h=c + 1, low=c - 1, c=c, v=2000.0)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            out_signal = out
            break
    assert out_signal is not None
    assert out_signal.side == "BUY"
    assert out_signal.regime == "crypto_scalp"


def test_scalp_long_blocked_when_rsi_below_threshold() -> None:
    """A break with RSI below the long-entry threshold should be rejected.

    Stream: extended downtrend (RSI well below 50) + a single upspike
    that pierces the rolling high. With rsi_long_min=80, the upspike's
    one-bar gain isn't enough to lift RSI above the gate.
    """
    s = _scalp(rsi_long_min=80.0, require_vwap_alignment=False)
    cfg = _config()
    hist: list[BarData] = []
    # Long downtrend so RSI saturates near 0
    for i in range(15):
        c = 110 - i * 0.5
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c, v=1000.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Up-spike that breaks the rolling high
    fired = False
    b = _bar(15, h=115, low=104, c=106, v=2000.0)
    hist.append(b)
    out = s.maybe_enter(b, hist, 10_000.0, cfg)
    if out is not None and out.side == "BUY":
        fired = True
    assert not fired, "long must be blocked when RSI < rsi_long_min"


def test_scalp_position_size_half_of_standard_risk() -> None:
    """risk_per_trade_pct defaults to 0.5% so qty = (eq*0.005)/stop_dist."""
    s = _scalp()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(8):
        c = 100 + i * 0.3
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c, v=1000.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    out_signal = None
    for i in range(8, 14):
        c = 110 + (i - 7) * 2
        b = _bar(i, h=c + 1, low=c - 1, c=c, v=2000.0)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            out_signal = out
            break
    assert out_signal is not None
    # risk = 0.005 * 10000 = 50; stop_dist = 0.8 * atr
    # We don't pin exact ATR; just confirm risk math is the half-rate.
    assert out_signal.risk_usd == pytest.approx(50.0, rel=1e-6)
