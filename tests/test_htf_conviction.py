"""Tests for HtfRegimeOracle + CryptoHtfConvictionStrategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.backtest.models import BacktestConfig
from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.crypto_htf_conviction_strategy import (
    CryptoHtfConvictionConfig,
    CryptoHtfConvictionStrategy,
    HtfConvictionSizingConfig,
    _conviction_multiplier,
)
from eta_engine.strategies.crypto_regime_trend_strategy import CryptoRegimeTrendConfig
from eta_engine.strategies.htf_regime_oracle import (
    HtfRegimeOracle,
    HtfRegimeOracleConfig,
)


def _bar(idx: int, *, h: float, low: float, c: float | None = None, v: float = 1000.0, tf_minutes: int = 60) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=idx * tf_minutes)
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


# ---------------------------------------------------------------------------
# Conviction multiplier
# ---------------------------------------------------------------------------


def test_conviction_multiplier_linear_ramp() -> None:
    cfg = HtfConvictionSizingConfig(
        base_multiplier=1.0,
        conviction_gain=1.0,
        min_size_multiplier=0.5,
        max_size_multiplier=2.0,
    )
    # Conv=0.5 → multiplier = 1.0 + 0 = 1.0
    assert _conviction_multiplier(cfg, 0.5) == pytest.approx(1.0)
    # Conv=1.0 → multiplier = 1.0 + 0.5 = 1.5
    assert _conviction_multiplier(cfg, 1.0) == pytest.approx(1.5)
    # Conv=0.0 → multiplier = 1.0 - 0.5 = 0.5
    assert _conviction_multiplier(cfg, 0.0) == pytest.approx(0.5)


def test_conviction_multiplier_caps_at_max() -> None:
    cfg = HtfConvictionSizingConfig(
        base_multiplier=1.0,
        conviction_gain=4.0,  # aggressive ramp
        min_size_multiplier=0.5,
        max_size_multiplier=2.0,
    )
    # Conv=1.0 with gain=4 → raw = 1.0 + 0.5*4 = 3.0; capped at 2.0
    assert _conviction_multiplier(cfg, 1.0) == pytest.approx(2.0)


def test_conviction_multiplier_floors_at_min() -> None:
    cfg = HtfConvictionSizingConfig(
        base_multiplier=1.0,
        conviction_gain=4.0,
        min_size_multiplier=0.5,
        max_size_multiplier=2.0,
    )
    # Conv=0.0 with gain=4 → raw = 1.0 - 0.5*4 = -1.0; floored at 0.5
    assert _conviction_multiplier(cfg, 0.0) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# HtfRegimeOracle
# ---------------------------------------------------------------------------


def test_oracle_neutral_when_no_providers_and_no_htf_ema() -> None:
    o = HtfRegimeOracle(
        HtfRegimeOracleConfig(
            weight_etf_flow=0.30,
            weight_htf_ema=0.25,
            weight_lth_proxy=0.15,
            weight_macro=0.15,
            weight_fear_greed=0.15,
            smoothing_period_days=0,
        )
    )
    bar = _bar(0, h=100, low=99, c=99.5)
    r = o.regime_for(bar)
    assert r.direction == "neutral"
    assert r.conviction == pytest.approx(0.0)


def test_oracle_long_when_etf_strongly_positive() -> None:
    """ETF inflow at 500M USD/day saturates the weight component."""
    o = HtfRegimeOracle(
        HtfRegimeOracleConfig(direction_threshold=0.10, smoothing_period_days=0),
        etf_flow_provider=lambda b: 500.0,  # full saturation
    )
    bar = _bar(0, h=100, low=99, c=99.5)
    r = o.regime_for(bar)
    assert r.direction == "long"
    # ETF weight 0.30 → composite = 1.0 * 0.30 / total_weight
    # All weights sum to 1.0 by default → composite ≈ 0.30
    assert r.composite == pytest.approx(0.30, abs=0.01)
    assert r.conviction == pytest.approx(0.30, abs=0.01)


def test_oracle_short_when_all_signals_bearish() -> None:
    o = HtfRegimeOracle(
        HtfRegimeOracleConfig(direction_threshold=0.10, smoothing_period_days=0),
        etf_flow_provider=lambda b: -500.0,
        lth_provider=lambda b: -1.0,
        fear_greed_provider=lambda b: -1.0,
        macro_provider=lambda b: -1.0,
    )
    # Set HTF EMA above price to push the htf_ema component bearish too
    o.update_htf_ema(200.0)  # bootstrap
    for _ in range(50):  # advance EMA toward 200
        o.update_htf_ema(200.0)
    bar = _bar(0, h=100, low=99, c=99.5)  # close=99.5 < ema=200
    r = o.regime_for(bar)
    assert r.direction == "short"
    assert r.composite < -0.5
    assert r.conviction > 0.5


def test_oracle_neutral_when_signals_cancel() -> None:
    """ETF positive, LTH negative, others zero → composite small."""
    o = HtfRegimeOracle(
        HtfRegimeOracleConfig(direction_threshold=0.20, smoothing_period_days=0),
        etf_flow_provider=lambda b: 500.0,  # +1
        lth_provider=lambda b: -1.0,  # -1
    )
    bar = _bar(0, h=100, low=99, c=99.5)
    r = o.regime_for(bar)
    # ETF (0.30) - LTH (0.15) = +0.15 net, below threshold 0.20
    assert r.direction == "neutral"
    assert r.composite == pytest.approx(0.15, abs=0.01)


def test_oracle_components_dict_populated() -> None:
    o = HtfRegimeOracle(
        HtfRegimeOracleConfig(),
        etf_flow_provider=lambda b: 250.0,
        lth_provider=lambda b: 0.5,
        fear_greed_provider=lambda b: 0.3,
    )
    bar = _bar(0, h=100, low=99, c=99.5)
    r = o.regime_for(bar)
    assert "etf_flow" in r.components
    assert "lth_proxy" in r.components
    assert "fear_greed" in r.components
    assert "htf_ema" in r.components
    assert "macro" in r.components


def test_oracle_smoothing_dampens_single_day_spike() -> None:
    """A one-day +1 score should be smoothed to <0.5 if smoothing is on."""
    o = HtfRegimeOracle(
        HtfRegimeOracleConfig(smoothing_period_days=10, direction_threshold=0.0),
        etf_flow_provider=lambda b: 0.0,  # default neutral
    )
    # Prime with neutral days
    bar = _bar(0, h=100, low=99, c=99.5)
    for _ in range(10):
        o.regime_for(bar)
    # Now flip the provider to extreme positive
    o._etf = lambda b: 500.0
    r = o.regime_for(bar)
    # Smoothing should not have reached full conviction yet
    assert r.composite < 0.30


# ---------------------------------------------------------------------------
# CryptoHtfConvictionStrategy
# ---------------------------------------------------------------------------


def _strat(**oracle_overrides) -> CryptoHtfConvictionStrategy:  # type: ignore[no-untyped-def]
    base = CryptoRegimeTrendConfig(
        regime_ema=20,
        pullback_ema=5,
        warmup_bars=25,
        atr_period=5,
        min_bars_between_trades=0,
        pullback_tolerance_pct=2.0,
        max_trades_per_day=100,
    )
    oracle_kwargs = {
        "smoothing_period_days": 0,
        "direction_threshold": 0.10,
        "htf_ema_period": 0,  # disable in-strategy HTF EMA for test simplicity
    }
    oracle_kwargs.update(oracle_overrides)
    sizing = HtfConvictionSizingConfig(
        min_conviction_to_trade=0.10,
        base_multiplier=1.0,
        conviction_gain=1.0,
        min_size_multiplier=0.5,
        max_size_multiplier=2.0,
    )
    cfg = CryptoHtfConvictionConfig(
        base=base,
        oracle=HtfRegimeOracleConfig(**oracle_kwargs),
        sizing=sizing,
    )
    return CryptoHtfConvictionStrategy(cfg)


def _setup_uptrend(s: CryptoHtfConvictionStrategy, n: int = 35) -> list[BarData]:
    cfg = _config()
    hist: list[BarData] = []
    for i in range(n):
        c = 100 + i * 0.5
        b = _bar(i, h=c + 0.3, low=c - 0.3, c=c)
        hist.append(b)
        s.maybe_enter(b, hist, 10_000.0, cfg)
    return hist


def test_neutral_oracle_blocks_entry() -> None:
    """Oracle with no providers reports neutral → no fire."""
    s = _strat()
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None


def test_long_aligned_oracle_fires_with_size_scaling() -> None:
    """Oracle says long, base agrees → fire with conviction-scaled size."""
    s = _strat()
    s.attach_etf_flow_provider(lambda b: 500.0)  # full +1 ETF score
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    assert out.side == "BUY"
    assert "htf_conv_long" in out.regime
    # Conviction = 0.30 (only ETF active), multiplier = 1.0 + (0.30-0.5)*1.0 = 0.8
    # But min_size_multiplier=0.5, so 0.8 stands.
    # risk_usd should be base risk (100) × 0.8 = 80
    assert out.risk_usd == pytest.approx(80.0, rel=1e-3)


def test_high_conviction_increases_size_above_baseline() -> None:
    """All providers bullish → conviction near max → multiplier > 1."""
    s = _strat()
    s.attach_etf_flow_provider(lambda b: 500.0)
    s.attach_lth_provider(lambda b: 1.0)
    s.attach_fear_greed_provider(lambda b: 1.0)
    s.attach_macro_provider(lambda b: 1.0)
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is not None
    # With HTF EMA disabled (period=0) and weights summing to 1.0:
    #   composite = ETF(1)*0.30 + HTF(0)*0.25 + LTH(1)*0.15
    #             + F&G(1)*0.15 + Macro(1)*0.15 = 0.75
    # multiplier = 1.0 + (0.75 - 0.5) * 1.0 = 1.25
    # risk_usd = 100 × 1.25 = 125
    assert out.risk_usd == pytest.approx(125.0, rel=0.05)
    # Multiplier = 1.25 → "mid" band (>=1.0, <1.4)
    assert "htf_conv_long_mid" in out.regime


def test_oracle_disagreement_blocks_entry() -> None:
    """Base proposes long but oracle says short → no fire."""
    s = _strat()
    s.attach_etf_flow_provider(lambda b: -500.0)  # bearish
    s.attach_lth_provider(lambda b: -1.0)
    s.attach_fear_greed_provider(lambda b: -1.0)
    s.attach_macro_provider(lambda b: -1.0)
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    # Oracle says short, base proposes BUY → blocked
    assert out is None


def test_low_conviction_blocks_entry() -> None:
    """Bullish but conviction below min → blocked."""
    s = _strat()
    # Only enable ETF provider with weak signal
    s.attach_etf_flow_provider(lambda b: 50.0)  # tiny inflow → small score
    # Override the strategy's sizing config to require high conviction
    new_sizing = HtfConvictionSizingConfig(min_conviction_to_trade=0.95)
    new_cfg = CryptoHtfConvictionConfig(
        base=s.cfg.base,
        oracle=s.cfg.oracle,
        sizing=new_sizing,
    )
    s.cfg = new_cfg
    cfg = _config()
    hist = _setup_uptrend(s)
    pull_ema = s._base._pullback_ema
    pull_bar = _bar(35, h=pull_ema + 0.5, low=pull_ema - 0.05, c=pull_ema + 0.4)
    hist.append(pull_bar)
    out = s.maybe_enter(pull_bar, hist, 10_000.0, cfg)
    assert out is None
