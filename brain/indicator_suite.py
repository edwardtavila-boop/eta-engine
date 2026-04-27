"""
EVOLUTIONARY TRADING ALGO  //  brain.indicator_suite
========================================
Regime-aware feature-weighting suite.

The stock ``core.confluence_scorer.WEIGHT_TABLE`` is static: trend_bias is
always worth 3, vol_regime is always worth 2, etc. That is wrong in the
real world -- a TRENDING regime pays far more for trend_bias than a
RANGING regime does. This module adds a thin regime-adaptive layer on
top of the existing scorer so the confluence pipeline can pivot its
emphasis as ``brain.regime.classify_regime`` shifts its label.

Design contract
---------------
1. Pure dict + pydantic. No numpy.
2. Weights always sum to the same _TOTAL_WEIGHT used by the scorer (10.0)
   so ``total_score`` stays on the [0, 10] scale across regimes.
3. Deterministic: same regime -> same weights.
4. Orthogonal to the scorer. The scorer does its own normalization; this
   module only decides *how much* each normalized score counts.
5. TRANSITION / unknown regimes fall back to the scorer's default weights.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from eta_engine.brain.regime import RegimeType
from eta_engine.core.confluence_scorer import (
    WEIGHT_TABLE as _DEFAULT_WEIGHTS,
)
from eta_engine.core.confluence_scorer import (
    ConfluenceResult,
    score_confluence,
)

if TYPE_CHECKING:
    from eta_engine.features.base import FeatureResult

# ---------------------------------------------------------------------------
# Regime weight profiles
# ---------------------------------------------------------------------------

# Names MUST match confluence_scorer.WEIGHT_TABLE keys exactly.
_FEATURES: tuple[str, ...] = (
    "trend_bias",
    "vol_regime",
    "funding_skew",
    "onchain_delta",
    "sentiment",
)

# Each row sums to 10.0 to preserve the 0-10 total_score scale.
_REGIME_PROFILES: dict[RegimeType, dict[str, float]] = {
    RegimeType.TRENDING: {
        "trend_bias": 4.0,
        "vol_regime": 1.5,
        "funding_skew": 2.0,
        "onchain_delta": 1.5,
        "sentiment": 1.0,
    },
    RegimeType.RANGING: {
        "trend_bias": 1.5,
        "vol_regime": 3.0,
        "funding_skew": 2.0,
        "onchain_delta": 1.5,
        "sentiment": 2.0,
    },
    RegimeType.HIGH_VOL: {
        "trend_bias": 2.0,
        "vol_regime": 1.0,
        "funding_skew": 3.0,
        "onchain_delta": 2.0,
        "sentiment": 2.0,
    },
    RegimeType.LOW_VOL: {
        "trend_bias": 3.5,
        "vol_regime": 1.0,
        "funding_skew": 1.5,
        "onchain_delta": 2.0,
        "sentiment": 2.0,
    },
    RegimeType.CRISIS: {
        # CRISIS nearly silences confluence -- we want the kill-switch to act,
        # not a big leveraged trade. Every normalized score gets heavily
        # discounted. Total still sums to 10.0 but the payoff is spread
        # across dim weights; scorer will rarely cross TRADE threshold.
        "trend_bias": 1.0,
        "vol_regime": 1.0,
        "funding_skew": 3.0,
        "onchain_delta": 2.5,
        "sentiment": 2.5,
    },
    # TRANSITION falls back to the scorer's default weights (set below).
}


def _default_profile() -> dict[str, float]:
    """Mirror confluence_scorer.WEIGHT_TABLE; used for TRANSITION/unknown."""
    return dict(_DEFAULT_WEIGHTS)


# Fill in TRANSITION so weights_for() always returns a complete dict.
_REGIME_PROFILES[RegimeType.TRANSITION] = _default_profile()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class RegimeWeightProfile(BaseModel):
    """Serializable view of one regime's weights."""

    regime: RegimeType
    weights: dict[str, float] = Field(
        description="Feature -> weight. Sums to 10.0.",
    )
    total: float = Field(
        10.0,
        description="Sum of all weights. Kept at 10.0 for scale consistency.",
    )


def weights_for(regime: RegimeType) -> dict[str, float]:
    """Return the weight dict for a given regime.

    Falls back to default weights for any unknown regime label.
    """
    return dict(_REGIME_PROFILES.get(regime, _default_profile()))


def profile_for(regime: RegimeType) -> RegimeWeightProfile:
    """Return a full Pydantic profile (useful for journal + dashboard)."""
    w = weights_for(regime)
    return RegimeWeightProfile(regime=regime, weights=w, total=round(sum(w.values()), 4))


def all_profiles() -> list[RegimeWeightProfile]:
    """Enumerate every known regime profile (for audit/report purposes)."""
    return [profile_for(r) for r in RegimeType]


# ---------------------------------------------------------------------------
# Scorer integration
# ---------------------------------------------------------------------------


def score_confluence_regime_aware(
    *,
    trend_bias: float,
    vol_regime: float,
    funding_skew: float,
    onchain_delta: float,
    sentiment: float,
    regime: RegimeType,
) -> ConfluenceResult:
    """Regime-adjusted wrapper around ``score_confluence``.

    The stock scorer uses fixed weights from WEIGHT_TABLE. Instead we
    temporarily override that table with the regime-specific profile, run
    the scorer, and restore the defaults. The approach keeps all the
    scorer's normalization + leverage mapping logic identical -- only the
    factor weights shift.
    """
    import eta_engine.core.confluence_scorer as scorer  # noqa: PLC0415

    weights = weights_for(regime)
    total_new = sum(weights.values())

    original_table = dict(scorer.WEIGHT_TABLE)
    original_total = scorer._TOTAL_WEIGHT
    try:
        scorer.WEIGHT_TABLE.clear()
        scorer.WEIGHT_TABLE.update(weights)
        # _TOTAL_WEIGHT is module-level; overwrite so the scorer divides by
        # the regime-adjusted total.
        scorer._TOTAL_WEIGHT = total_new
        return score_confluence(
            trend_bias=trend_bias,
            vol_regime=vol_regime,
            funding_skew=funding_skew,
            onchain_delta=onchain_delta,
            sentiment=sentiment,
        )
    finally:
        scorer.WEIGHT_TABLE.clear()
        scorer.WEIGHT_TABLE.update(original_table)
        scorer._TOTAL_WEIGHT = original_total


def weighted_confluence_tuple(
    results: dict[str, FeatureResult],
    regime: RegimeType,
) -> tuple[float, float, float, float, float]:
    """Return the 5-tuple of ``normalized_score * regime_weight_fraction``.

    Useful when the caller wants a per-feature contribution vector rather
    than a full ConfluenceResult. The fraction = weight / 10.0 so values
    stay in [0, 1] per slot and sum to the confluence mean.
    """
    weights = weights_for(regime)
    total = sum(weights.values()) or 1.0
    return tuple(  # type: ignore[return-value]
        (results[name].normalized_score if name in results else 0.0) * (weights.get(name, 0.0) / total)
        for name in _FEATURES
    )
