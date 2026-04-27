"""
EVOLUTIONARY TRADING ALGO  //  tests.test_features
======================================
Feature pipeline + individual feature scorers.
Each feature must return [0, 1]; pipeline must compose to 5-tuple.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.core.data_pipeline import BarData, FundingRate
from eta_engine.features import (
    FeaturePipeline,
    FundingSkewFeature,
    OnchainFeature,
    SentimentFeature,
    TrendBiasFeature,
    VolRegimeFeature,
    atr_percentile,
    contrarian_extreme_score,
    ema_slope_score,
    whale_delta_score,
)


@pytest.fixture()
def bar() -> BarData:
    return BarData(
        timestamp=datetime.now(UTC),
        symbol="ETHUSDT",
        open=3500.0,
        high=3510.0,
        low=3495.0,
        close=3505.0,
        volume=1234.0,
    )


@pytest.fixture()
def ctx() -> dict:
    return {
        "daily_ema": [3400.0, 3420.0, 3450.0, 3470.0, 3500.0],
        "h4_struct": "HH_HL",
        "bias": 1,
        "atr_history": [20.0, 22.0, 25.0, 28.0, 30.0, 32.0, 35.0],
        "atr_current": 26.0,
        "funding_history": [FundingRate(timestamp=datetime.now(UTC), symbol="ETHUSDT", rate=0.0001) for _ in range(8)],
        "onchain": {
            "whale_transfers": 12,
            "whale_transfers_baseline": 6,
            "exchange_netflow_usd": -20_000_000.0,
            "active_addresses": 1200,
            "active_addresses_baseline": 1000,
        },
        "sentiment": {
            "galaxy_score": 85.0,
            "alt_rank": 15,
            "social_volume": 500,
            "social_volume_baseline": 200,
            "fear_greed": 22,
        },
    }


class TestHelpers:
    def test_ema_slope_rising(self) -> None:
        assert ema_slope_score([100, 101, 102, 103]) > 0.5

    def test_ema_slope_empty(self) -> None:
        assert ema_slope_score([]) == 0.5

    def test_atr_percentile(self) -> None:
        p = atr_percentile([1, 2, 3, 4, 5], 3)
        assert 0.0 <= p <= 1.0
        assert p == 0.4

    def test_whale_delta_positive(self) -> None:
        assert whale_delta_score(10, 5) > 0.5

    def test_contrarian_extreme_divergence(self) -> None:
        assert contrarian_extreme_score(85.0, 20) == 1.0
        assert contrarian_extreme_score(50.0, 50) == 0.0


class TestFeatures:
    @pytest.mark.parametrize(
        "feature",
        [
            TrendBiasFeature(),
            VolRegimeFeature(),
            FundingSkewFeature(),
            OnchainFeature(),
            SentimentFeature(),
        ],
    )
    def test_feature_returns_bounded_float(self, feature: object, bar: BarData, ctx: dict) -> None:
        score = feature.compute(bar, ctx)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_feature_evaluate_wraps_result(self, bar: BarData, ctx: dict) -> None:
        result = TrendBiasFeature().evaluate(bar, ctx)
        assert result.name == "trend_bias"
        assert result.weight == 3.0
        assert 0.0 <= result.normalized_score <= 1.0


class TestPipeline:
    def test_default_has_five_features(self) -> None:
        p = FeaturePipeline.default()
        assert len(p._features) == 5  # noqa: SLF001

    def test_compute_all_returns_keyed_results(self, bar: BarData, ctx: dict) -> None:
        p = FeaturePipeline.default()
        results = p.compute_all(bar, ctx)
        assert set(results) == {"trend_bias", "vol_regime", "funding_skew", "onchain_delta", "sentiment"}

    def test_to_confluence_inputs_returns_five_tuple(self, bar: BarData, ctx: dict) -> None:
        p = FeaturePipeline.default()
        results = p.compute_all(bar, ctx)
        inputs = p.to_confluence_inputs(results)
        assert len(inputs) == 5
        assert all(isinstance(v, float) and 0.0 <= v <= 1.0 for v in inputs)

    @pytest.mark.asyncio
    async def test_refresh_external_fans_out(self) -> None:
        p = FeaturePipeline.default()
        snap = await p.refresh_external("ETH")
        assert set(snap) == {"onchain", "sentiment"}
        assert snap["onchain"]["asset"] == "ETH"
        assert snap["sentiment"]["asset"] == "ETH"
