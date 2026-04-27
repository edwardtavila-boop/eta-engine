"""Tests for crypto_macro_confluence_strategy + providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 - tmp_path fixture annotation

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.crypto_macro_confluence_strategy import (
    CryptoMacroConfluenceConfig,
    CryptoMacroConfluenceStrategy,
    MacroConfluenceConfig,
)
from eta_engine.strategies.crypto_regime_trend_strategy import CryptoRegimeTrendConfig
from eta_engine.strategies.macro_confluence_providers import (
    EtfFlowProvider,
    EthAlignmentProvider,
    FearGreedProvider,
    FundingRateProvider,
    LthProxyProvider,
    MacroTailwindProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(idx: int, *, h: float, low: float, c: float | None = None,
         v: float = 1000.0, tf_minutes: int = 60,
         hour_override: int | None = None) -> BarData:
    """Synthetic bar at 2026-01-01 + idx*tf_minutes UTC."""
    if hour_override is not None:
        ts = datetime(2026, 1, 1, hour_override, 0, tzinfo=UTC) + timedelta(days=idx)
    else:
        ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=idx * tf_minutes)
    c = c if c is not None else (h + low) / 2
    return BarData(
        timestamp=ts, symbol="BTC", open=(h + low) / 2,
        high=h, low=low, close=c, volume=v,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        symbol="BTC", initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=0.0,
        max_trades_per_day=10,
    )


def _strat(**filter_overrides) -> CryptoMacroConfluenceStrategy:  # type: ignore[no-untyped-def]
    """Small base + opt-in filter overrides; tests stay fast."""
    base = CryptoRegimeTrendConfig(
        regime_ema=20, pullback_ema=5, warmup_bars=25,
        atr_period=5, min_bars_between_trades=0,
        pullback_tolerance_pct=2.0, max_trades_per_day=100,
    )
    filters = MacroConfluenceConfig(**filter_overrides)
    return CryptoMacroConfluenceStrategy(
        CryptoMacroConfluenceConfig(base=base, filters=filters),
    )


def _setup_uptrend(s: CryptoMacroConfluenceStrategy, n: int = 35) -> list[BarData]:
    cfg = _config()
    hist: list[BarData] = []
    for i in range(n):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    return hist


# ---------------------------------------------------------------------------
# Pass-through (no filters enabled)
# ---------------------------------------------------------------------------


def test_passthrough_when_all_filters_disabled() -> None:
    """No filters enabled → behaves like crypto_regime_trend baseline."""
    s = _strat()
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    assert pull_ema is not None
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert "macro_conf" in out.regime  # confluence-confirmed regime tag


# ---------------------------------------------------------------------------
# Variant A: HTF EMA alignment
# ---------------------------------------------------------------------------


def test_htf_alignment_blocks_when_close_below_htf_ema() -> None:
    """Price > short regime EMA but < HTF EMA → block long."""
    s = _strat(htf_ema_period=200)  # very slow EMA
    cfg = _config()
    hist: list[BarData] = []
    # First, set HTF EMA HIGH with a long high-price warmup
    for i in range(50):
        c = 1000.0  # constant high price
        b = _bar(i, h=c + 1, low=c - 1, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    # Then a sharp drop + small uptrend at low price
    for i in range(50, 90):
        c = 100 + (i - 50) * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    pull_ema = s._base._pullback_ema
    assert pull_ema is not None
    htf_ema = s._htf_ema
    assert htf_ema is not None
    # HTF EMA should still be high (slow to update); current price is low
    assert htf_ema > 500
    pull_bar = _bar(90, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    # Long should be blocked by HTF filter (close < HTF EMA)
    assert out is None


# ---------------------------------------------------------------------------
# Variant B: time-of-day window
# ---------------------------------------------------------------------------


def test_time_of_day_blocks_outside_window() -> None:
    """Bar outside the allow_utc_hours set → no fire."""
    s = _strat(allow_utc_hours=frozenset({13, 14, 15}))
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    assert pull_ema is not None
    # Bar at hour 5 (Asian session) — outside the allow set
    asian_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05,
                     c=pull_ema + 0.4, hour_override=5)
    hist.append(asian_bar)
    out = s.maybe_enter(asian_bar, hist, 10_000.0, cfg)
    assert out is None


def test_time_of_day_passes_inside_window() -> None:
    s = _strat(allow_utc_hours=frozenset({13, 14, 15}))
    cfg = _config()
    hist: list[BarData] = []
    # Build setup at hour 14 each day so the strategy fires inside window
    base_dt = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    for i in range(35):
        c = 100 + i * 0.5
        ts = base_dt + timedelta(days=i)
        b = BarData(timestamp=ts, symbol="BTC", open=c, high=c + 0.3,
                    low=c - 0.3, close=c, volume=1000.0)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    pull_ema = s._base._pullback_ema
    assert pull_ema is not None
    # Pull bar at hour 14
    ts = base_dt + timedelta(days=35)
    pull_bar = BarData(
        timestamp=ts, symbol="BTC",
        open=pull_ema, high=pull_ema + 0.5,
        low=pull_ema - 0.05, close=pull_ema + 0.4, volume=1000.0,
    )
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None


# ---------------------------------------------------------------------------
# Variant D: ETH alignment provider
# ---------------------------------------------------------------------------


def test_eth_alignment_fail_closed_without_provider() -> None:
    """Filter on but no provider attached → fail closed."""
    s = _strat(require_eth_alignment=True)
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None  # no provider → fail closed


def test_eth_alignment_passes_when_eth_aligned() -> None:
    s = _strat(require_eth_alignment=True)
    cfg = _config()
    s.attach_eth_alignment_provider(lambda b: 1.0)  # ETH always bullish
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"


def test_eth_alignment_blocks_when_eth_opposite() -> None:
    s = _strat(require_eth_alignment=True)
    cfg = _config()
    s.attach_eth_alignment_provider(lambda b: -1.0)  # ETH bearish
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


# ---------------------------------------------------------------------------
# Variant E: funding rate filter
# ---------------------------------------------------------------------------


def test_funding_filter_blocks_extreme_positive_for_long() -> None:
    """Crowded longs (funding > threshold) → block long."""
    s = _strat(extreme_funding_threshold=0.001)  # 0.1%
    cfg = _config()
    s.attach_funding_provider(lambda b: 0.005)  # 0.5%, very crowded
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


def test_funding_filter_passes_neutral_funding() -> None:
    s = _strat(extreme_funding_threshold=0.001)
    cfg = _config()
    s.attach_funding_provider(lambda b: 0.0001)  # near zero
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None


# ---------------------------------------------------------------------------
# Variant F: macro tailwind
# ---------------------------------------------------------------------------


def test_macro_tailwind_blocks_long_on_negative_score() -> None:
    s = _strat(min_macro_score=0.3)
    cfg = _config()
    s.attach_macro_provider(lambda b: -0.5)  # macro headwind
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


def test_macro_tailwind_passes_long_on_positive_score() -> None:
    s = _strat(min_macro_score=0.3)
    cfg = _config()
    s.attach_macro_provider(lambda b: 0.5)  # macro tailwind
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None


# ---------------------------------------------------------------------------
# Provider unit tests
# ---------------------------------------------------------------------------


def test_eth_alignment_provider_returns_zero_when_no_data() -> None:
    p = EthAlignmentProvider(eth_bars=[], regime_ema_period=10)
    bar = _bar(0, h=100, low=99, c=99.5)
    assert p(bar) == 0.0


def test_funding_provider_handles_missing_csv(tmp_path: Path) -> None:
    p = FundingRateProvider(csv_path=tmp_path / "no_such.csv")
    bar = _bar(0, h=100, low=99, c=99.5)
    assert p(bar) == 0.0


def test_macro_provider_handles_missing_csvs(tmp_path: Path) -> None:
    p = MacroTailwindProvider(
        dxy_csv=tmp_path / "no_dxy.csv",
        spy_csv=tmp_path / "no_spy.csv",
    )
    bar = _bar(0, h=100, low=99, c=99.5)
    assert p(bar) == 0.0


def test_funding_provider_round_trip(tmp_path: Path) -> None:
    csv_p = tmp_path / "fund.csv"
    csv_p.write_text(
        "time,funding_rate\n"
        f"{int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())},0.0001\n"
        f"{int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())},0.0005\n",
        encoding="utf-8",
    )
    p = FundingRateProvider(csv_path=csv_p)
    # Bar between the two readings — should return the first one
    bar = _bar(0, h=100, low=99, c=99.5)  # 2026-01-01 00:00
    assert p(bar) == pytest.approx(0.0001)
    # Bar after both — returns the latest
    bar2 = BarData(
        timestamp=datetime(2026, 1, 3, tzinfo=UTC),
        symbol="BTC", open=100, high=100, low=100, close=100, volume=1000,
    )
    assert p(bar2) == pytest.approx(0.0005)


# ---------------------------------------------------------------------------
# Tier-4 providers
# ---------------------------------------------------------------------------


def test_etf_flow_provider_returns_zero_when_no_csv(tmp_path: Path) -> None:
    p = EtfFlowProvider(csv_path=tmp_path / "no_etf.csv")
    bar = _bar(0, h=100, low=99, c=99.5)
    assert p(bar) == 0.0


def test_etf_flow_provider_returns_latest_at_or_before_bar(tmp_path: Path) -> None:
    csv_p = tmp_path / "etf.csv"
    csv_p.write_text(
        "time,net_flow_usd_m\n"
        f"{int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())},250.5\n"
        f"{int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())},-180.0\n",
        encoding="utf-8",
    )
    p = EtfFlowProvider(csv_path=csv_p)
    # Bar on Jan 1 → first day's flow
    bar = BarData(
        timestamp=datetime(2026, 1, 1, 12, tzinfo=UTC),
        symbol="BTC", open=100, high=100, low=100, close=100, volume=1000,
    )
    assert p(bar) == pytest.approx(250.5)
    # Bar on Jan 2 → second day's flow (outflow)
    bar2 = BarData(
        timestamp=datetime(2026, 1, 2, 12, tzinfo=UTC),
        symbol="BTC", open=100, high=100, low=100, close=100, volume=1000,
    )
    assert p(bar2) == pytest.approx(-180.0)


def test_fear_greed_provider_normalizes_to_signed_range(tmp_path: Path) -> None:
    csv_p = tmp_path / "fg.csv"
    # Three days: extreme fear (10), neutral (50), extreme greed (90)
    csv_p.write_text(
        "time,fear_greed\n"
        f"{int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())},10\n"
        f"{int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())},50\n"
        f"{int(datetime(2026, 1, 3, tzinfo=UTC).timestamp())},90\n",
        encoding="utf-8",
    )
    p = FearGreedProvider(csv_path=csv_p)
    fear_bar = BarData(
        timestamp=datetime(2026, 1, 1, 12, tzinfo=UTC),
        symbol="BTC", open=100, high=100, low=100, close=100, volume=1000,
    )
    neutral_bar = BarData(
        timestamp=datetime(2026, 1, 2, 12, tzinfo=UTC),
        symbol="BTC", open=100, high=100, low=100, close=100, volume=1000,
    )
    greed_bar = BarData(
        timestamp=datetime(2026, 1, 3, 12, tzinfo=UTC),
        symbol="BTC", open=100, high=100, low=100, close=100, volume=1000,
    )
    # Contrarian: fear maps to +0.8, neutral to 0, greed to -0.8
    assert p(fear_bar) == pytest.approx(0.8, abs=1e-3)
    assert p(neutral_bar) == pytest.approx(0.0, abs=1e-3)
    assert p(greed_bar) == pytest.approx(-0.8, abs=1e-3)


def test_lth_proxy_provider_round_trip(tmp_path: Path) -> None:
    csv_p = tmp_path / "lth.csv"
    csv_p.write_text(
        "time,lth_proxy\n"
        f"{int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())},0.75\n"
        f"{int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())},-0.40\n",
        encoding="utf-8",
    )
    p = LthProxyProvider(csv_path=csv_p)
    bar = BarData(
        timestamp=datetime(2026, 1, 1, 12, tzinfo=UTC),
        symbol="BTC", open=100, high=100, low=100, close=100, volume=1000,
    )
    assert p(bar) == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Tier-4 strategy filters
# ---------------------------------------------------------------------------


def test_lth_filter_blocks_long_when_score_too_low() -> None:
    s = _strat(min_lth_score=0.3)
    cfg = _config()
    s.attach_onchain_provider(lambda b: -0.5)  # distribution phase
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


def test_lth_filter_passes_long_in_accumulation() -> None:
    s = _strat(min_lth_score=0.3)
    cfg = _config()
    s.attach_onchain_provider(lambda b: 0.6)  # accumulation phase
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None


def test_sentiment_filter_blocks_long_in_greed() -> None:
    s = _strat(min_sentiment_score=0.2)
    cfg = _config()
    s.attach_sentiment_provider(lambda b: -0.5)  # extreme greed (contrarian)
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


def test_sentiment_filter_passes_long_in_fear() -> None:
    s = _strat(min_sentiment_score=0.2)
    cfg = _config()
    s.attach_sentiment_provider(lambda b: 0.5)  # fear (contrarian = bullish)
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None


def test_etf_filter_blocks_long_on_outflow() -> None:
    s = _strat(require_etf_flow_alignment=True)
    cfg = _config()
    s.attach_etf_flow_provider(lambda b: -250.0)  # net outflow
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


def test_etf_filter_passes_long_on_inflow() -> None:
    s = _strat(require_etf_flow_alignment=True)
    cfg = _config()
    s.attach_etf_flow_provider(lambda b: 350.0)  # institutional inflow
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
