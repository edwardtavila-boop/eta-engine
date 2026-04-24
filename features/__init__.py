"""
APEX PREDATOR  //  features
===========================
Feature engineering surface for the confluence scorer.
5 factors → 1 weighted score → leverage decision.
"""

from apex_predator.features.base import Feature, FeatureResult
from apex_predator.features.funding_skew import (
    FundingSkewFeature,
    cumulative_funding_8h,
)
from apex_predator.features.onchain import (
    OnchainFeature,
    fetch_onchain_snapshot,
    whale_delta_score,
)
from apex_predator.features.pipeline import FeaturePipeline
from apex_predator.features.regime_hmm_feature import (
    RegimeHMMFeature,
    build_hmm_ctx,
)
from apex_predator.features.sentiment import (
    SentimentFeature,
    contrarian_extreme_score,
    fetch_sentiment_snapshot,
)
from apex_predator.features.trend_bias import TrendBiasFeature, ema_slope_score
from apex_predator.features.vol_regime import VolRegimeFeature, atr_percentile

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
