"""Tests for FeatureRegimeClassifier — multi-feature regime
classifier that replaces the failed price-EMA classifier.

Built for the 2026-04-27 user-question follow-on: "with all the
data we have how do we optimize regime?". The price-EMA classifier
failed because its axis didn't carve BTC's tape along the
strategy's edge axis. This classifier scores ACTUAL signal
features (funding, ETF flow, F&G, sage daily).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eta_engine.core.data_pipeline import BarData
from eta_engine.strategies.feature_regime_classifier import (
    FeatureRegimeClassifier,
    FeatureRegimeConfig,
    make_feature_regime_provider,
)


def _bar(idx: int, close: float = 100.0) -> BarData:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=idx)
    return BarData(
        timestamp=ts, symbol="BTC", open=close,
        high=close + 1.0, low=close - 1.0, close=close, volume=1000.0,
    )


# ---------------------------------------------------------------------------
# Single-feature behavior
# ---------------------------------------------------------------------------


def test_neutral_when_no_providers_attached() -> None:
    """No providers → score 0, label neutral."""
    c = FeatureRegimeClassifier()
    out = c.classify(_bar(0))
    assert out.score == 0.0
    assert out.label == "neutral"
    assert out.bias == "neutral"
    assert out.n_features_active == 0


def test_funding_positive_extreme_bearish_score() -> None:
    """Funding > +threshold → bearish state (-1) for that feature."""
    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=True, use_etf_flow=False,
        use_fear_greed=False, use_sage_daily=False,
        funding_extreme=0.0005, bull_threshold=0.5, bear_threshold=0.5,
    ))
    c.attach_funding_provider(lambda b: 0.001)  # extreme positive
    out = c.classify(_bar(0))
    assert out.components["funding_state"] == -1.0
    assert out.score == -1.0
    assert out.label == "bear_aligned"


def test_funding_negative_extreme_bullish_score() -> None:
    """Funding < -threshold → bullish state (+1)."""
    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=True, use_etf_flow=False,
        use_fear_greed=False, use_sage_daily=False,
        funding_extreme=0.0005, bull_threshold=0.5, bear_threshold=0.5,
    ))
    c.attach_funding_provider(lambda b: -0.001)
    out = c.classify(_bar(0))
    assert out.components["funding_state"] == +1.0
    assert out.score == +1.0
    assert out.label == "bull_aligned"


def test_etf_flow_rolling_sum_threshold() -> None:
    """ETF flow rolling-window sum drives the state."""
    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=False, use_etf_flow=True,
        use_fear_greed=False, use_sage_daily=False,
        etf_flow_window_days=3, etf_flow_threshold=300.0,
        bull_threshold=0.5, bear_threshold=0.5,
    ))
    # Simulate 200M USD/day inflow for 3 days = 600M total > 300M
    flows = [200.0, 200.0, 200.0, 200.0, 200.0]
    iter_state = iter(flows)

    def _provider(b):  # noqa: ANN001, ANN202
        return next(iter_state)

    c.attach_etf_flow_provider(_provider)
    last_out = None
    for i in range(5):
        b = BarData(
            timestamp=datetime(2026, 1, 1 + i, tzinfo=UTC),
            symbol="BTC", open=100.0, high=101.0, low=99.0,
            close=100.0, volume=1000.0,
        )
        last_out = c.classify(b)
    assert last_out is not None
    assert last_out.components["etf_state"] == +1.0
    assert last_out.label == "bull_aligned"


def test_fear_greed_extreme_fear_is_bullish() -> None:
    """Contrarian-flipped F&G: extreme fear (+0.6+) is bullish."""
    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=False, use_etf_flow=False,
        use_fear_greed=True, use_sage_daily=False,
        fear_greed_extreme=0.6,
        bull_threshold=0.5, bear_threshold=0.5,
    ))
    c.attach_fear_greed_provider(lambda b: 0.8)  # extreme fear
    out = c.classify(_bar(0))
    assert out.components["fear_greed_state"] == +1.0
    assert out.label == "bull_aligned"


def test_sage_long_with_high_conviction_bullish() -> None:
    @dataclass
    class V:
        direction: str
        conviction: float

    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=False, use_etf_flow=False,
        use_fear_greed=False, use_sage_daily=True,
        sage_conviction_floor=0.30,
        bull_threshold=0.5, bear_threshold=0.5,
    ))
    c.attach_sage_daily_provider(lambda d: V(direction="long", conviction=0.5))
    out = c.classify(_bar(0))
    assert out.components["sage_state"] == +1.0
    assert out.label == "bull_aligned"


def test_sage_low_conviction_neutral() -> None:
    """Sage's direction is ignored if conviction below floor."""
    @dataclass
    class V:
        direction: str
        conviction: float

    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=False, use_etf_flow=False,
        use_fear_greed=False, use_sage_daily=True,
        sage_conviction_floor=0.30,
        bull_threshold=0.5, bear_threshold=0.5,
    ))
    c.attach_sage_daily_provider(lambda d: V(direction="long", conviction=0.1))
    out = c.classify(_bar(0))
    assert out.components["sage_state"] == 0.0
    assert out.label == "neutral"


# ---------------------------------------------------------------------------
# Multi-feature composition
# ---------------------------------------------------------------------------


def test_score_normalized_by_enabled_feature_count() -> None:
    """Score is sum / n_enabled, bounded to [-1, +1]."""
    @dataclass
    class V:
        direction: str
        conviction: float

    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=True, use_etf_flow=False,
        use_fear_greed=False, use_sage_daily=True,
        funding_extreme=0.0005, sage_conviction_floor=0.3,
        bull_threshold=0.30, bear_threshold=0.30,
    ))
    c.attach_funding_provider(lambda b: -0.001)  # +1
    c.attach_sage_daily_provider(lambda d: V("long", 0.5))  # +1
    out = c.classify(_bar(0))
    # 2 features enabled, both +1, score = 2/2 = +1.0
    assert abs(out.score - 1.0) < 1e-6
    assert out.label == "bull_aligned"


def test_disagreement_yields_neutral() -> None:
    """Two features disagreeing should produce a near-zero score."""
    @dataclass
    class V:
        direction: str
        conviction: float

    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=True, use_etf_flow=False,
        use_fear_greed=False, use_sage_daily=True,
        funding_extreme=0.0005, sage_conviction_floor=0.3,
        bull_threshold=0.5, bear_threshold=0.5,
    ))
    c.attach_funding_provider(lambda b: -0.001)  # +1 (capitulated shorts)
    c.attach_sage_daily_provider(lambda d: V("short", 0.5))  # -1
    out = c.classify(_bar(0))
    # Net 0 → neutral
    assert abs(out.score) < 1e-6
    assert out.label == "neutral"


def test_regime_distribution_audit() -> None:
    """Stats counters track classify() invocations."""
    c = FeatureRegimeClassifier()
    for i in range(10):
        c.classify(_bar(i))
    assert c.n_classified == 10
    assert sum(c.regime_distribution.values()) == 10


# ---------------------------------------------------------------------------
# Provider-builder adapter
# ---------------------------------------------------------------------------


def test_provider_adapter_returns_classification_objects() -> None:
    """``make_feature_regime_provider`` should produce a callable
    that takes a date and returns an HtfRegimeClassification."""
    c = FeatureRegimeClassifier(FeatureRegimeConfig(
        use_funding=False, use_etf_flow=False,
        use_fear_greed=False, use_sage_daily=False,
    ))
    daily_bars = [_bar(i * 24, close=100.0 + i) for i in range(5)]
    provider = make_feature_regime_provider(c, daily_bars)
    out = provider(daily_bars[2].timestamp.date())
    assert hasattr(out, "regime")
    assert hasattr(out, "bias")
    assert hasattr(out, "mode")
    assert out.bias in {"long", "short", "neutral"}


def test_provider_adapter_handles_pre_coverage_dates() -> None:
    """Date before any classification → safe-veto neutral output."""
    from datetime import date as date_t

    c = FeatureRegimeClassifier()
    daily_bars = [_bar(i * 24, close=100.0 + i) for i in range(5)]
    provider = make_feature_regime_provider(c, daily_bars)
    out = provider(date_t(2020, 1, 1))
    assert out.regime == "volatile"
    assert out.bias == "neutral"
    assert out.mode == "skip"
