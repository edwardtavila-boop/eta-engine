"""Tests for eta_engine.brain.indicator_suite -- regime-aware weighting."""

from __future__ import annotations

import pytest

from eta_engine.brain.indicator_suite import (
    RegimeWeightProfile,
    all_profiles,
    profile_for,
    score_confluence_regime_aware,
    weighted_confluence_tuple,
    weights_for,
)
from eta_engine.brain.regime import RegimeType
from eta_engine.core.confluence_scorer import (
    WEIGHT_TABLE as DEFAULT_WEIGHTS,
)
from eta_engine.core.confluence_scorer import (
    score_confluence,
)
from eta_engine.features.base import FeatureResult

# --------------------------------------------------------------------------- #
# weights_for
# --------------------------------------------------------------------------- #


def test_weights_for_every_regime_sums_to_ten() -> None:
    for r in RegimeType:
        w = weights_for(r)
        assert round(sum(w.values()), 4) == 10.0, f"{r} sums to {sum(w.values())}"


def test_weights_for_every_regime_has_all_five_features() -> None:
    expected = {"trend_bias", "vol_regime", "funding_skew", "onchain_delta", "sentiment"}
    for r in RegimeType:
        assert set(weights_for(r).keys()) == expected, r


def test_weights_for_trending_emphasizes_trend_bias() -> None:
    w = weights_for(RegimeType.TRENDING)
    assert w["trend_bias"] == max(w.values())
    # Heavier than the default 3.0
    assert w["trend_bias"] > DEFAULT_WEIGHTS["trend_bias"]


def test_weights_for_ranging_de_emphasizes_trend_bias() -> None:
    w = weights_for(RegimeType.RANGING)
    assert w["trend_bias"] < DEFAULT_WEIGHTS["trend_bias"]
    # Vol regime (mean-reversion-friendly) gets the bump
    assert w["vol_regime"] > DEFAULT_WEIGHTS["vol_regime"]


def test_weights_for_high_vol_upweights_funding_and_sentiment() -> None:
    w = weights_for(RegimeType.HIGH_VOL)
    assert w["funding_skew"] > DEFAULT_WEIGHTS["funding_skew"]
    assert w["sentiment"] > DEFAULT_WEIGHTS["sentiment"]


def test_weights_for_crisis_suppresses_trend_bias() -> None:
    w = weights_for(RegimeType.CRISIS)
    # Trend bias doesn't dominate during crisis -- something else does
    assert w["trend_bias"] <= 1.5


def test_weights_for_transition_matches_defaults() -> None:
    assert weights_for(RegimeType.TRANSITION) == dict(DEFAULT_WEIGHTS)


def test_weights_for_returns_independent_copy() -> None:
    w = weights_for(RegimeType.TRENDING)
    w["trend_bias"] = 99.0
    # Second call must not see the mutation
    assert weights_for(RegimeType.TRENDING)["trend_bias"] != 99.0


# --------------------------------------------------------------------------- #
# profile_for + all_profiles
# --------------------------------------------------------------------------- #


def test_profile_for_returns_pydantic_model() -> None:
    p = profile_for(RegimeType.TRENDING)
    assert isinstance(p, RegimeWeightProfile)
    assert p.regime == RegimeType.TRENDING
    assert round(p.total, 4) == 10.0


def test_all_profiles_covers_every_regime() -> None:
    profiles = all_profiles()
    assert {p.regime for p in profiles} == set(RegimeType)
    for p in profiles:
        assert round(sum(p.weights.values()), 4) == 10.0


# --------------------------------------------------------------------------- #
# score_confluence_regime_aware
# --------------------------------------------------------------------------- #


def test_score_regime_aware_trending_beats_default_on_strong_trend() -> None:
    # Strong trend, weak other factors. Trending profile should reward this
    # more than the default profile.
    kwargs = {
        "trend_bias": 1.0,
        "vol_regime": 0.3,
        "funding_skew": 0.0,
        "onchain_delta": 0.1,
        "sentiment": 0.5,
    }
    default_res = score_confluence(**kwargs)
    trending_res = score_confluence_regime_aware(regime=RegimeType.TRENDING, **kwargs)
    assert trending_res.total_score > default_res.total_score


def test_score_regime_aware_ranging_beats_default_on_mean_reversion() -> None:
    # Weak trend but strong mean-reversion signals (funding extreme, sentiment
    # extreme, healthy vol). Ranging profile should reward this.
    kwargs = {
        "trend_bias": 0.0,
        "vol_regime": 0.5,
        "funding_skew": 0.001,
        "onchain_delta": 0.5,
        "sentiment": 0.05,  # extreme fear -> contrarian bull
    }
    default_res = score_confluence(**kwargs)
    ranging_res = score_confluence_regime_aware(regime=RegimeType.RANGING, **kwargs)
    assert ranging_res.total_score > default_res.total_score


def test_score_regime_aware_crisis_scores_lower_than_default() -> None:
    # Even with a "good" signal set, CRISIS should not hand out leverage
    kwargs = {
        "trend_bias": 1.0,
        "vol_regime": 0.5,
        "funding_skew": 0.001,
        "onchain_delta": 1.0,
        "sentiment": 0.9,
    }
    default_res = score_confluence(**kwargs)
    crisis_res = score_confluence_regime_aware(regime=RegimeType.CRISIS, **kwargs)
    # Crisis either trims the score or at least doesn't raise it
    assert crisis_res.total_score <= default_res.total_score


def test_score_regime_aware_returns_full_confluence_result_shape() -> None:
    res = score_confluence_regime_aware(
        trend_bias=0.5,
        vol_regime=0.5,
        funding_skew=0.0005,
        onchain_delta=0.5,
        sentiment=0.5,
        regime=RegimeType.TRENDING,
    )
    assert hasattr(res, "total_score")
    assert hasattr(res, "recommended_leverage")
    assert hasattr(res, "signal")
    assert res.signal in {"TRADE", "REDUCE", "NO_TRADE"}
    # 5 factors in the breakdown
    assert len(res.factors) == 5


def test_score_regime_aware_restores_default_weights_after_call() -> None:
    # Pre-capture defaults
    import eta_engine.core.confluence_scorer as scorer  # noqa: PLC0415

    before = dict(scorer.WEIGHT_TABLE)
    before_total = scorer._TOTAL_WEIGHT

    score_confluence_regime_aware(
        trend_bias=0.5,
        vol_regime=0.5,
        funding_skew=0.0005,
        onchain_delta=0.5,
        sentiment=0.5,
        regime=RegimeType.CRISIS,
    )
    assert dict(scorer.WEIGHT_TABLE) == before
    assert before_total == scorer._TOTAL_WEIGHT


def test_score_regime_aware_restores_defaults_even_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eta_engine.core.confluence_scorer as scorer  # noqa: PLC0415

    before = dict(scorer.WEIGHT_TABLE)

    def boom(**_kw: object) -> None:
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(
        "eta_engine.brain.indicator_suite.score_confluence",
        boom,
    )
    with pytest.raises(RuntimeError, match="exploded"):
        score_confluence_regime_aware(
            trend_bias=0.5,
            vol_regime=0.5,
            funding_skew=0.0005,
            onchain_delta=0.5,
            sentiment=0.5,
            regime=RegimeType.TRENDING,
        )
    # Weights still restored
    assert dict(scorer.WEIGHT_TABLE) == before


# --------------------------------------------------------------------------- #
# weighted_confluence_tuple
# --------------------------------------------------------------------------- #


def _fake_result(name: str, score: float) -> FeatureResult:
    return FeatureResult(
        name=name,
        raw_value=score,
        normalized_score=score,
        weight=1.0,
    )


def test_weighted_confluence_tuple_returns_five_floats() -> None:
    results = {
        "trend_bias": _fake_result("trend_bias", 0.8),
        "vol_regime": _fake_result("vol_regime", 0.6),
        "funding_skew": _fake_result("funding_skew", 0.4),
        "onchain_delta": _fake_result("onchain_delta", 0.5),
        "sentiment": _fake_result("sentiment", 0.5),
    }
    t = weighted_confluence_tuple(results, RegimeType.TRENDING)
    assert len(t) == 5
    assert all(isinstance(v, float) for v in t)
    # Each slot is in [0, 1]
    for v in t:
        assert 0.0 <= v <= 1.0


def test_weighted_confluence_tuple_upscales_trend_in_trending() -> None:
    # One perfect trend-bias score, everything else zero. Verify the
    # slot-0 value is proportional to the regime weight for trend_bias.
    results = {
        "trend_bias": _fake_result("trend_bias", 1.0),
        "vol_regime": _fake_result("vol_regime", 0.0),
        "funding_skew": _fake_result("funding_skew", 0.0),
        "onchain_delta": _fake_result("onchain_delta", 0.0),
        "sentiment": _fake_result("sentiment", 0.0),
    }
    trending = weighted_confluence_tuple(results, RegimeType.TRENDING)
    ranging = weighted_confluence_tuple(results, RegimeType.RANGING)
    # Trend bias slot (index 0) should be larger under TRENDING than under RANGING
    assert trending[0] > ranging[0]


def test_weighted_confluence_tuple_handles_missing_features() -> None:
    # Only 2 of the 5 features populated -- rest should be 0
    results = {
        "trend_bias": _fake_result("trend_bias", 1.0),
        "sentiment": _fake_result("sentiment", 1.0),
    }
    t = weighted_confluence_tuple(results, RegimeType.TRENDING)
    # Index 0 = trend_bias, 4 = sentiment, others zero
    assert t[0] > 0.0
    assert t[4] > 0.0
    assert t[1] == 0.0
    assert t[2] == 0.0
    assert t[3] == 0.0
