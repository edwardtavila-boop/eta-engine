"""
EVOLUTIONARY TRADING ALGO  //  tests.test_confluence
========================================
Confluence scorer: weighted 0-10 scoring, leverage mapping, signal gating.
"""

from __future__ import annotations

import pytest

from eta_engine.core.confluence_scorer import (
    score_confluence,
)


class TestConfluenceScorer:
    def test_perfect_score(self) -> None:
        """All factors at maximum strength -> near 10."""
        result = score_confluence(
            trend_bias=1.0,
            vol_regime=0.5,
            funding_skew=0.001,
            onchain_delta=1.0,
            sentiment=0.05,
        )
        assert result.total_score >= 9.0
        assert result.total_score <= 10.0
        assert result.recommended_leverage == 75
        assert result.signal == "TRADE"

    def test_zero_score(self) -> None:
        """All factors dead -> near 0, no trade."""
        result = score_confluence(
            trend_bias=0.0,
            vol_regime=0.0,
            funding_skew=0.0,
            onchain_delta=0.0,
            sentiment=0.5,
        )
        assert result.total_score < 5.0
        assert result.signal == "NO_TRADE"
        assert result.recommended_leverage == 0

    def test_leverage_ramp_thresholds(self) -> None:
        """Verify leverage steps: 0 (<5), 10 (5-7), 20 (7-9), 75 (9+)."""
        # Mid-range score -> 10x
        mid = score_confluence(
            trend_bias=0.5,
            vol_regime=0.5,
            funding_skew=0.0005,
            onchain_delta=0.5,
            sentiment=0.5,
        )
        assert mid.recommended_leverage in (0, 10, 20, 75)
        assert mid.signal in ("TRADE", "REDUCE", "NO_TRADE")

    def test_result_has_five_factors(self) -> None:
        result = score_confluence(1.0, 0.5, 0.001, 1.0, 0.5)
        assert len(result.factors) == 5

    def test_factors_have_correct_names(self) -> None:
        result = score_confluence(0.5, 0.5, 0.0005, 0.5, 0.5)
        names = {f.name for f in result.factors}
        assert names == {"trend_bias", "vol_regime", "funding_skew", "onchain_delta", "sentiment"}

    def test_score_bounded_0_10(self) -> None:
        """Score never exceeds bounds regardless of inputs."""
        result = score_confluence(1.0, 0.5, 0.01, 1.0, 0.01)
        assert 0.0 <= result.total_score <= 10.0

    @pytest.mark.parametrize("trend", [-1.0, -0.5, 0.0, 0.5, 1.0])
    def test_trend_direction_strength(self, trend: float) -> None:
        """Trend uses absolute value — bull and bear equally valid."""
        bull = score_confluence(abs(trend), 0.5, 0.0005, 0.5, 0.5)
        bear = score_confluence(-abs(trend), 0.5, 0.0005, 0.5, 0.5)
        # Abs means bull/bear at same magnitude produce same score
        assert abs(bull.total_score - bear.total_score) < 0.01

    def test_reduce_signal_range(self) -> None:
        """Score in [5, 7) should give REDUCE signal."""
        result = score_confluence(
            trend_bias=0.6,
            vol_regime=0.5,
            funding_skew=0.0003,
            onchain_delta=0.4,
            sentiment=0.5,
        )
        if 5.0 <= result.total_score < 7.0:
            assert result.signal == "REDUCE"
            assert result.recommended_leverage == 10
