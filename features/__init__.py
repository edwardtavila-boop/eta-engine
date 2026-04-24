"""
EVOLUTIONARY TRADING ALGO  //  features
===========================
Feature engineering surface for the confluence scorer.
5 factors → 1 weighted score → leverage decision.
"""

from eta_engine.features.base import Feature, FeatureResult
from eta_engine.features.funding_skew import (
    FundingSkewFeature,
    cumulative_funding_8h,
)
from eta_engine.features.onchain import (
    OnchainFeature,
    fetch_onchain_snapshot,
    whale_delta_score,
)
from eta_engine.features.pipeline import FeaturePipeline
from eta_engine.features.regime_hmm_feature import (
    RegimeHMMFeature,
    build_hmm_ctx,
)
from eta_engine.features.sentiment import (
    SentimentFeature,
    contrarian_extreme_score,
    fetch_sentiment_snapshot,
)
from eta_engine.features.trend_bias import TrendBiasFeature, ema_slope_score
from eta_engine.features.vol_regime import VolRegimeFeature, atr_percentile

__all__ = [
    "Feature",
    "FeaturePipeline",
    "FeatureResult",
    "FundingSkewFeature",
    "OnchainFeature",
    "RegimeHMMFeature",
    "SentimentFeature",
    "TrendBiasFeature",
    "VolRegimeFeature",
    "atr_percentile",
    "build_hmm_ctx",
    "contrarian_extreme_score",
    "cumulative_funding_8h",
    "ema_slope_score",
    "fetch_onchain_snapshot",
    "fetch_sentiment_snapshot",
    "whale_delta_score",
]
