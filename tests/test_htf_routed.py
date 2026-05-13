"""Tests for HtfRoutedStrategy + MeanRevertSubStrategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.crypto_regime_trend_strategy import CryptoRegimeTrendConfig
from eta_engine.strategies.htf_regime_classifier import HtfRegimeClassification
from eta_engine.strategies.htf_routed_strategy import (
    HtfRoutedConfig,
    HtfRoutedStrategy,
    MeanRevertConfig,
    MeanRevertSubStrategy,
)


def _bar(idx: int, *, h: float, low: float, c: float | None = None, v: float = 1000.0) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=idx)
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
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _make_classification(
    mode: str = "trend_follow",
    bias: str = "long",
    regime: str = "trending",
) -> HtfRegimeClassification:
    return HtfRegimeClassification(
        bias=bias,
        regime=regime,
        mode=mode,
        close=100.0,
        fast_ema=99.0,
        slow_ema=98.0,
    )


# ---------------------------------------------------------------------------
# MeanRevertSubStrategy
# ---------------------------------------------------------------------------


def _mr(**overrides) -> MeanRevertSubStrategy:  # type: ignore[no-untyped-def]
    base = {
        "regime_ema": 10,
        "warmup_bars": 15,
        "atr_period": 5,
        "min_bars_between_trades": 0,
        "extreme_distance_pct": 1.5,
    }
    base.update(overrides)
    return MeanRevertSubStrategy(MeanRevertConfig(**base))


def test_mean_revert_warmup_blocks() -> None:
    s = _mr()
    cfg = _config()
    assert s.maybe_enter(_bar(0, h=100, low=99, c=99.5), [], 10_000.0, cfg) is None


def test_mean_revert_long_on_lower_extreme_pierce() -> None:
    """Bar's low pierces ≥1.5% below regime EMA + close back inside → BUY."""
    s = _mr()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i, h=100.5, low=99.5, c=100.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    ema = s._regime_ema
    assert ema is not None
    # Build a bar that wicks 2% below EMA but closes back inside band
    pierce_bar = _bar(20, h=ema, low=ema * 0.98, c=ema * 0.995)
    hist.append(pierce_bar)
    out = s.maybe_enter(pierce_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert "mean_revert" in out.regime


def test_mean_revert_short_on_upper_extreme_pierce() -> None:
    s = _mr()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i, h=100.5, low=99.5, c=100.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    ema = s._regime_ema
    rip_bar = _bar(20, h=ema * 1.02, low=ema, c=ema * 1.005)
    hist.append(rip_bar)
    out = s.maybe_enter(rip_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "SELL"


def test_mean_revert_no_fire_when_close_outside_band() -> None:
    """If close is OUTSIDE the band (didn't return), no mean-rev signal."""
    s = _mr()
    cfg = _config()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i, h=100.5, low=99.5, c=100.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    ema = s._regime_ema
    # Wick AND close below band: outright breakdown, not mean-rev
    fail_bar = _bar(20, h=ema, low=ema * 0.97, c=ema * 0.97)
    hist.append(fail_bar)
    out = s.maybe_enter(fail_bar, hist, 10_000.0, cfg)
    assert out is None


# ---------------------------------------------------------------------------
# HtfRoutedStrategy
# ---------------------------------------------------------------------------


def _router(mode: str = "trend_follow", bias: str = "long", regime: str = "trending") -> tuple[HtfRoutedStrategy, list]:  # type: ignore[type-arg]
    """Build a router strategy + a mutable list for swapping classifications."""
    cfg = HtfRoutedConfig(
        trend_follow=CryptoRegimeTrendConfig(
            regime_ema=20,
            pullback_ema=5,
            warmup_bars=25,
            atr_period=5,
            min_bars_between_trades=0,
            pullback_tolerance_pct=2.0,
            max_trades_per_day=100,
        ),
        mean_revert=MeanRevertConfig(
            regime_ema=10,
            warmup_bars=15,
            atr_period=5,
            min_bars_between_trades=0,
            extreme_distance_pct=1.5,
            max_trades_per_day=100,
        ),
        enforce_htf_bias_alignment=True,
        honor_htf_skip=True,
    )
    s = HtfRoutedStrategy(cfg)
    classification_holder = [_make_classification(mode=mode, bias=bias, regime=regime)]
    s.attach_htf_classification_provider(lambda b: classification_holder[0])
    return s, classification_holder


def test_router_no_provider_means_no_trade() -> None:
    """Without an HTF provider attached, fail-closed (no trades)."""
    cfg = HtfRoutedConfig()
    s = HtfRoutedStrategy(cfg)
    cfg_b = _config()
    hist: list[BarData] = []
    for i in range(40):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg_b)
        assert out is None


def test_router_skip_mode_blocks_all_entries() -> None:
    s, holder = _router(mode="skip", bias="neutral", regime="volatile")
    cfg = _config()
    hist: list[BarData] = []
    fired = False
    for i in range(50):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        out = s.maybe_enter(b, hist, 10_000.0, cfg)
        if out is not None:
            fired = True
            break
    assert not fired


def test_router_trend_follow_fires_when_htf_long_and_pullback() -> None:
    s, holder = _router(mode="trend_follow", bias="long", regime="trending")
    cfg = _config()
    hist: list[BarData] = []
    # Build uptrend so trend sub-strategy gets a pullback signal
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    pull_ema = s._trend._pullback_ema
    assert pull_ema is not None
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert "htf_routed_trend_follow_long_trending" in out.regime


def test_router_blocks_short_when_htf_says_long() -> None:
    s, holder = _router(mode="trend_follow", bias="long", regime="trending")
    cfg = _config()
    hist: list[BarData] = []
    # Build DOWNTREND so trend sub-strategy proposes a SHORT
    for i in range(35):
        c = 100 - i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    pull_ema = s._trend._pullback_ema
    assert pull_ema is not None
    rip_bar = _bar(35, h=pull_ema + 0.05, low=pull_ema - 0.5, c=pull_ema - 0.4)
    hist.append(rip_bar)
    out = s.maybe_enter(rip_bar, hist, 10_000.0, cfg)
    # Trend sub-strategy proposes SELL but HTF bias=long → veto
    assert out is None


def test_router_mean_revert_mode_fires_within_range() -> None:
    s, holder = _router(mode="mean_revert", bias="neutral", regime="ranging")
    cfg = _config()
    hist: list[BarData] = []
    for i in range(20):
        b = _bar(i, h=100.5, low=99.5, c=100.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    ema = s._mean_revert._regime_ema
    assert ema is not None
    pierce_bar = _bar(20, h=ema, low=ema * 0.98, c=ema * 0.995)
    hist.append(pierce_bar)
    out = s.maybe_enter(pierce_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert "htf_routed_mean_revert" in out.regime


def test_router_mode_switches_when_classification_changes() -> None:
    """Start in trend mode, switch to skip — second bar must not fire."""
    s, holder = _router(mode="trend_follow", bias="long", regime="trending")
    cfg = _config()
    hist: list[BarData] = []
    for i in range(35):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    pull_ema = s._trend._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out_trend = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out_trend is not None
    # Now classification flips to skip; next bar with same shape → no fire
    holder[0] = _make_classification(mode="skip", bias="neutral", regime="volatile")
    pull_bar2 = _bar(36, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar2)
    out_skip = s.maybe_enter(pull_bar2, hist, 10_000.0, cfg)
    assert out_skip is None
