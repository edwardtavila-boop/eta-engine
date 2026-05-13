"""Tests for HtfRegimeClassifier + PiCycleStrategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.htf_regime_classifier import (
    HtfRegimeClassifier,
    HtfRegimeClassifierConfig,
)
from eta_engine.strategies.pi_cycle_strategy import (
    PiCycleConfig,
    PiCycleStrategy,
)


def _bar(idx: int, *, h: float, low: float, c: float | None = None, v: float = 1000.0) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=idx)
    c = c if c is not None else (h + low) / 2
    return BarData(
        timestamp=ts,
        symbol="BTC",
        open=(h + low) / 2,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2030, 12, 31, tzinfo=UTC),
        symbol="BTC",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.02,
        confluence_threshold=0.0,
        max_trades_per_day=1,
    )


# ---------------------------------------------------------------------------
# HtfRegimeClassifier
# ---------------------------------------------------------------------------


def _classifier(**overrides) -> HtfRegimeClassifier:  # type: ignore[no-untyped-def]
    base = {
        "fast_ema": 5,
        "slow_ema": 20,
        "slope_lookback": 5,
        "warmup_bars": 30,
        "atr_period": 5,
        "trend_distance_pct": 3.0,
        "range_atr_pct_max": 2.0,
        "slope_threshold_pct": 0.5,
    }
    base.update(overrides)
    return HtfRegimeClassifier(HtfRegimeClassifierConfig(**base))


def test_classifier_warmup_returns_skip() -> None:
    c = _classifier(warmup_bars=20)
    bar = _bar(0, h=100, low=99, c=99.5)
    c.update(bar)
    out = c.classify(bar)
    assert out.bias == "neutral"
    assert out.regime == "volatile"
    assert out.mode == "skip"


def test_classifier_long_trending_in_strong_uptrend() -> None:
    """Sharp persistent uptrend → long bias + trending regime."""
    c = _classifier()
    last = None
    for i in range(40):
        price = 100 + i * 5  # +5% per bar — strong uptrend
        bar = _bar(i, h=price + 0.5, low=price - 0.5, c=price)
        c.update(bar)
        last = c.classify(bar)
    assert last is not None
    assert last.bias == "long"
    assert last.regime == "trending"
    assert last.mode == "trend_follow"


def test_classifier_short_trending_in_strong_downtrend() -> None:
    c = _classifier()
    last = None
    for i in range(40):
        price = 200 - i * 5  # strong downtrend
        bar = _bar(i, h=price + 0.5, low=price - 0.5, c=price)
        c.update(bar)
        last = c.classify(bar)
    assert last is not None
    assert last.bias == "short"
    assert last.regime == "trending"
    assert last.mode == "trend_follow"


def test_classifier_ranging_in_flat_chop() -> None:
    """Tight chop around 100 → ranging + mean_revert mode."""
    c = _classifier(range_atr_pct_max=5.0)  # accept chop ATR
    last = None
    for i in range(40):
        price = 100 + ((-1) ** i) * 0.2  # tiny oscillation
        bar = _bar(i, h=price + 0.1, low=price - 0.1, c=price)
        c.update(bar)
        last = c.classify(bar)
    assert last is not None
    # Bias likely neutral (slope flat); regime ranging; mode mean_revert
    assert last.regime == "ranging"
    assert last.mode == "mean_revert"


def test_classifier_skip_when_volatile() -> None:
    """Wide-range bars near slow EMA → volatile + skip."""
    c = _classifier(range_atr_pct_max=1.0)  # easy to exceed
    last = None
    for i in range(40):
        # Price oscillates wildly around 100 with wide bars
        price = 100 + ((-1) ** i) * 0.5
        bar = _bar(i, h=price + 8, low=price - 8, c=price)  # ATR ~16, ATR%=16%
        c.update(bar)
        last = c.classify(bar)
    assert last is not None
    assert last.regime == "volatile"
    assert last.mode == "skip"


def test_classifier_components_populated() -> None:
    c = _classifier()
    for i in range(40):
        bar = _bar(i, h=100 + i * 0.5, low=99 + i * 0.5, c=99.5 + i * 0.5)
        c.update(bar)
        out = c.classify(bar)
    assert "fast_ema" in out.components
    assert "slow_ema" in out.components
    assert "slope_pct" in out.components
    assert "distance_pct" in out.components
    assert "atr_pct" in out.components


# ---------------------------------------------------------------------------
# PiCycleStrategy
# ---------------------------------------------------------------------------


def _pi(**overrides) -> PiCycleStrategy:  # type: ignore[no-untyped-def]
    """Pi Cycle with small SMAs + multiplier=1.0 so tests run fast.

    Production uses multiplier=2.0 (Pi Cycle's classic spec — fits
    BTC's bull-cycle dynamics where 111-SMA accelerates ~2x the
    350-SMA at peak). For unit tests we use 1.0 to make the
    crossover purely a fast-vs-slow comparison that's verifiable
    by hand on synthetic data.
    """
    base = {
        "fast_sma_period": 5,
        "slow_sma_period": 15,
        "fast_sma_multiplier": 1.0,  # tests use 1.0; production = 2.0
        "atr_period": 5,
        "cooldown_bars": 5,
    }
    base.update(overrides)
    return PiCycleStrategy(PiCycleConfig(**base))


def test_pi_cycle_warmup_no_signal() -> None:
    s = _pi()
    cfg = _config()
    bar = _bar(0, h=100, low=99, c=99.5)
    assert s.maybe_enter(bar, [], 10_000.0, cfg) is None


def test_pi_cycle_top_signal_fires_short() -> None:
    """Build series where 5-SMA × 2 crosses ABOVE 15-SMA → SELL."""
    s = _pi()
    cfg = _config()
    hist: list[BarData] = []
    # Long sideways at 50 to anchor 15-SMA near 50
    for i in range(20):
        b = _bar(i, h=50 + 0.5, low=50 - 0.5, c=50)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Sharp surge → 5-SMA accelerates much faster than 15-SMA
    out = None
    for i in range(20, 40):
        c = 50 + (i - 19) * 5  # rapid rally
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c)
        hist.append(b)
        result = s.maybe_enter(b, hist, 10_000.0, cfg)
        if result is not None:
            out = result
            break
    assert out is not None, "Pi Cycle top should fire a SELL on rapid rally"
    assert out.side == "SELL"
    assert out.regime == "pi_cycle_top"


def test_pi_cycle_bottom_signal_fires_long() -> None:
    """Sharp crash → 5-SMA × 2 crosses BELOW 15-SMA → BUY."""
    s = _pi()
    cfg = _config()
    hist: list[BarData] = []
    # Build at 100 with 15-SMA stable near 100
    for i in range(20):
        b = _bar(i, h=100 + 0.5, low=100 - 0.5, c=100)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Crash: 5-SMA drops fast; need 5-SMA × 2 < 15-SMA which requires
    # 5-SMA < 15-SMA / 2 (so 5-SMA < 50 if 15-SMA ≈ 100)
    out = None
    for i in range(20, 40):
        c = 100 - (i - 19) * 5  # rapid decline
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c)
        hist.append(b)
        result = s.maybe_enter(b, hist, 10_000.0, cfg)
        if result is not None:
            out = result
            break
    assert out is not None, "Pi Cycle bottom should fire a BUY on crash"
    assert out.side == "BUY"
    assert out.regime == "pi_cycle_bottom"


def test_pi_cycle_top_signal_can_be_disabled() -> None:
    s = _pi(enable_top_signal=False)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i, h=50 + 0.5, low=50 - 0.5, c=50)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    fired_top = False
    for i in range(20, 40):
        c = 50 + (i - 19) * 5
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None and out.side == "SELL":
            fired_top = True
            break
    assert not fired_top, "top signal must be skipped when disabled"


def test_pi_cycle_cooldown_blocks_immediate_re_fire() -> None:
    s = _pi(cooldown_bars=10)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i, h=50 + 0.5, low=50 - 0.5, c=50)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # First fire on rapid rally
    first = None
    for i in range(20, 40):
        c = 50 + (i - 19) * 5
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c)
        hist.append(b)
        result = s.maybe_enter(b, hist, 10_000.0, cfg)
        if result is not None:
            first = result
            break
    assert first is not None
    # Now run a few more bars while still in cooldown — even if a
    # crossover happens, no fire should occur.
    fires_during_cooldown = 0
    for i in range(40, 45):
        c = 200 - (i - 40) * 20  # sharp reversal
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c)
        hist.append(b)
        result = s.maybe_enter(b, hist, 10_000.0, cfg)
        if result is not None:
            fires_during_cooldown += 1
    assert fires_during_cooldown == 0


def test_pi_cycle_position_size_uses_pct() -> None:
    s = _pi(risk_per_trade_pct=0.02, atr_stop_mult=4.0)
    cfg = _config()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i, h=50 + 0.5, low=50 - 0.5, c=50)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    out = None
    for i in range(20, 40):
        c = 50 + (i - 19) * 5
        b = _bar(i, h=c + 0.5, low=c - 0.5, c=c)
        hist.append(b)
        result = s.maybe_enter(b, hist, 10_000.0, cfg)
        if result is not None:
            out = result
            break
    assert out is not None
    # risk = 2% × 10000 = 200
    assert out.risk_usd == pytest.approx(200.0, rel=1e-6)
