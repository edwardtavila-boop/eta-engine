"""
EVOLUTIONARY TRADING ALGO  //  features.pipeline
====================================
Wire all 5 features into one pipeline.
Compose → score → feed to confluence_scorer.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from eta_engine.features.funding_skew import FundingSkewFeature
from eta_engine.features.onchain import OnchainFeature, fetch_onchain_snapshot
from eta_engine.features.sentiment import SentimentFeature, fetch_sentiment_snapshot
from eta_engine.features.trend_bias import TrendBiasFeature
from eta_engine.features.vol_regime import VolRegimeFeature

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData
    from eta_engine.features.base import Feature, FeatureResult

ConfluenceTuple = tuple[float, float, float, float, float]


class FeaturePipeline:
    """Orchestrates the 5 confluence features end-to-end."""

    _DEFAULT_ORDER: tuple[str, ...] = (
        "trend_bias",
        "vol_regime",
        "funding_skew",
        "onchain_delta",
        "sentiment",
    )

    def __init__(self) -> None:
        self._features: dict[str, Feature] = {}

    @classmethod
    def default(cls) -> FeaturePipeline:
        """Pipeline prewired with the 5 stock features."""
        p = cls()
        p.register(TrendBiasFeature())
        p.register(VolRegimeFeature())
        p.register(FundingSkewFeature())
        p.register(OnchainFeature())
        p.register(SentimentFeature())
        return p

    def register(self, feature: Feature) -> None:
        """Add a feature. Name collisions overwrite."""
        self._features[feature.name] = feature

    def compute_all(
        self,
        bar: BarData,
        ctx: dict[str, Any],
    ) -> dict[str, FeatureResult]:
        """Compute every registered feature and return keyed results."""
        return {name: feat.evaluate(bar, ctx) for name, feat in self._features.items()}

    def to_confluence_inputs(
        self,
        results: dict[str, FeatureResult],
    ) -> ConfluenceTuple:
        """Project results dict into the 5-tuple score_confluence expects.

        Returns (trend_bias, vol_regime, funding_skew, onchain_delta, sentiment).
        Missing features default to 0.0.
        """
        return tuple(results[name].normalized_score if name in results else 0.0 for name in self._DEFAULT_ORDER)  # type: ignore[return-value]

    async def refresh_external(self, asset: str) -> dict[str, dict[str, Any]]:
        """Fan out async fetches for onchain + sentiment snapshots in parallel."""
        onchain, sentiment = await asyncio.gather(
            fetch_onchain_snapshot(asset),
            fetch_sentiment_snapshot(asset),
        )
        return {"onchain": onchain, "sentiment": sentiment}
