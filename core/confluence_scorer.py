"""
EVOLUTIONARY TRADING ALGO  //  confluence_scorer
=====================================
0-10 weighted confluence scoring.
Score drives leverage. No score, no trade.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ConfluenceFactor(BaseModel):
    """Single scoring dimension."""

    name: str
    weight: float = Field(gt=0, description="Weight in total score")
    raw_value: float = Field(description="Raw input value before normalization")
    normalized_score: float = Field(ge=0.0, le=1.0, description="Normalized to [0, 1]")


class ConfluenceResult(BaseModel):
    """Aggregated confluence output."""

    factors: list[ConfluenceFactor]
    total_score: float = Field(ge=0.0, le=10.0)
    recommended_leverage: int = Field(ge=0)
    signal: Literal["TRADE", "REDUCE", "NO_TRADE"]


# ---------------------------------------------------------------------------
# Weight table
# ---------------------------------------------------------------------------

WEIGHT_TABLE: dict[str, float] = {
    "trend_bias": 3.0,
    "vol_regime": 2.0,
    "funding_skew": 2.0,
    "onchain_delta": 1.5,
    "sentiment": 1.5,
}

_TOTAL_WEIGHT = sum(WEIGHT_TABLE.values())  # 10.0


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _normalize_trend_bias(raw: float) -> float:
    """Trend bias: -1 (strong bear) to +1 (strong bull). Abs → strength."""
    return _clamp(abs(raw))


def _normalize_vol_regime(raw: float) -> float:
    """Vol regime: 0=dead, 1=normal, 2+=extreme. Sweet spot 0.3-0.8."""
    if raw < 0.3:
        return _clamp(raw / 0.3 * 0.5)
    if raw <= 0.8:
        return 1.0
    return _clamp(1.0 - (raw - 0.8) / 1.2)


def _normalize_funding_skew(raw: float) -> float:
    """Funding rate: -0.1% to +0.1%. Extreme = opportunity."""
    return _clamp(abs(raw) / 0.001)


def _normalize_onchain(raw: float) -> float:
    """On-chain delta: 0=no signal, 1=strong confirmation."""
    return _clamp(raw)


def _normalize_sentiment(raw: float) -> float:
    """Sentiment: 0=max fear, 1=max greed. Contrarian at extremes."""
    if raw < 0.15 or raw > 0.85:
        return 1.0  # extreme = contrarian signal
    return _clamp(0.5 + abs(raw - 0.5))


_NORMALIZERS: dict[str, callable] = {
    "trend_bias": _normalize_trend_bias,
    "vol_regime": _normalize_vol_regime,
    "funding_skew": _normalize_funding_skew,
    "onchain_delta": _normalize_onchain,
    "sentiment": _normalize_sentiment,
}


# ---------------------------------------------------------------------------
# Leverage mapping
# ---------------------------------------------------------------------------


def _score_to_leverage(score: float) -> int:
    if score >= 9.0:
        return 75
    if score >= 7.0:
        return 20
    if score >= 5.0:
        return 10
    return 0


def _score_to_signal(score: float) -> Literal["TRADE", "REDUCE", "NO_TRADE"]:
    if score >= 7.0:
        return "TRADE"
    if score >= 5.0:
        return "REDUCE"
    return "NO_TRADE"


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------


def score_confluence(
    trend_bias: float,
    vol_regime: float,
    funding_skew: float,
    onchain_delta: float,
    sentiment: float,
) -> ConfluenceResult:
    """Score 5 confluence factors into a 0-10 composite.

    Args:
        trend_bias:    -1.0 (bear) to +1.0 (bull)
        vol_regime:    0.0 (dead) to 2.0+ (extreme)
        funding_skew:  Funding rate as decimal (e.g. 0.0003)
        onchain_delta: 0.0 (no signal) to 1.0 (strong)
        sentiment:     0.0 (fear) to 1.0 (greed)

    Returns:
        ConfluenceResult with weighted score, leverage, and signal.
    """
    raw_inputs = {
        "trend_bias": trend_bias,
        "vol_regime": vol_regime,
        "funding_skew": funding_skew,
        "onchain_delta": onchain_delta,
        "sentiment": sentiment,
    }

    factors: list[ConfluenceFactor] = []
    weighted_sum = 0.0

    for name, raw in raw_inputs.items():
        norm = _NORMALIZERS[name](raw)
        weight = WEIGHT_TABLE[name]
        factors.append(
            ConfluenceFactor(
                name=name,
                weight=weight,
                raw_value=raw,
                normalized_score=round(norm, 4),
            )
        )
        weighted_sum += norm * weight

    total = round(weighted_sum / _TOTAL_WEIGHT * 10.0, 2)
    total = min(total, 10.0)

    return ConfluenceResult(
        factors=factors,
        total_score=total,
        recommended_leverage=_score_to_leverage(total),
        signal=_score_to_signal(total),
    )


# ---------------------------------------------------------------------------
# MNQ-tuned scorer
# ---------------------------------------------------------------------------
# MNQ futures don't have a funding rate, on-chain transfers, or a
# Galaxy Score equivalent. Including those features at any weight
# either (a) penalises the score when their inputs are correctly zero
# (futures aren't crypto), or (b) requires the ctx_builder to inject
# synthetic favorable values (which masks the bar-derived signal).
#
# This MNQ-tuned scorer drops them. Total weight collapses from 10
# to 5; trend_bias + vol_regime alignment alone can clear the 7.0
# entry threshold. Any future MNQ-specific features (volume regime,
# CME basis, ES correlation) should be added to MNQ_WEIGHT_TABLE,
# not the global one.

MNQ_WEIGHT_TABLE: dict[str, float] = {
    "trend_bias": 3.0,
    "vol_regime": 2.0,
    # Crypto-only features intentionally absent.
}
_MNQ_TOTAL_WEIGHT = sum(MNQ_WEIGHT_TABLE.values())  # 5.0


# ---------------------------------------------------------------------------
# BTC-tuned scorer (CME-aware)
# ---------------------------------------------------------------------------
# CME crypto futures (BTC, MBT, ETH, MET) are cash-settled to the CF
# Reference Rate. They have NO native funding rate and NO on-chain
# settlement — but spot crypto is what they're priced off, and spot is
# moved by funding pressure + on-chain activity + sentiment. So all
# five features remain signal-bearing, just with equal weighting
# rather than the crypto-perp emphasis you'd want for native perp
# trading. When the ctx doesn't carry funding/onchain (e.g. when only
# CME bars are available without a paired perp feed), those inputs
# default to 0 and the scorer still produces a meaningful score from
# the bar-derived features.

BTC_WEIGHT_TABLE: dict[str, float] = {
    "trend_bias": 2.0,
    "vol_regime": 2.0,
    "funding_skew": 2.0,
    "onchain_delta": 2.0,
    "sentiment": 2.0,
}
_BTC_TOTAL_WEIGHT = sum(BTC_WEIGHT_TABLE.values())  # 10.0


def score_confluence_btc(
    trend_bias: float,
    vol_regime: float,
    funding_skew: float = 0.0,
    onchain_delta: float = 0.0,
    sentiment: float = 0.0,
) -> ConfluenceResult:
    """BTC-tuned confluence scorer (CME + perp friendly).

    Equal weighting across the five features. Default zeros for
    funding/onchain/sentiment so a caller with only CME bars (no
    paired perp/spot/sentiment feed) still produces a valid score
    from the bar-derived signals. When the perp-side feeds ARE
    available (live BTC perp trading), passing real values gives
    them meaningful contribution to the composite.
    """
    raw_inputs = {
        "trend_bias": trend_bias,
        "vol_regime": vol_regime,
        "funding_skew": funding_skew,
        "onchain_delta": onchain_delta,
        "sentiment": sentiment,
    }
    factors: list[ConfluenceFactor] = []
    weighted_sum = 0.0
    for name, raw in raw_inputs.items():
        weight = BTC_WEIGHT_TABLE.get(name, 0.0)
        if weight <= 0.0:
            continue
        norm = _NORMALIZERS[name](raw)
        factors.append(
            ConfluenceFactor(
                name=name,
                weight=weight,
                raw_value=raw,
                normalized_score=round(norm, 4),
            )
        )
        weighted_sum += norm * weight
    total = round(weighted_sum / _BTC_TOTAL_WEIGHT * 10.0, 2)
    total = min(total, 10.0)
    return ConfluenceResult(
        factors=factors,
        total_score=total,
        recommended_leverage=_score_to_leverage(total),
        signal=_score_to_signal(total),
    )


def score_confluence_mnq(
    trend_bias: float,
    vol_regime: float,
    funding_skew: float = 0.0,  # kept for signature compat with the engine
    onchain_delta: float = 0.0,
    sentiment: float = 0.0,
) -> ConfluenceResult:
    """MNQ-tuned variant of :func:`score_confluence`.

    Uses only ``trend_bias`` and ``vol_regime``; the remaining args
    exist solely so this function is a drop-in for the 5-tuple that
    ``FeaturePipeline.to_confluence_inputs`` returns. Their values
    are ignored.

    Returns a :class:`ConfluenceResult` whose ``factors`` list still
    enumerates all five inputs (with weight 0 for the dropped ones)
    so dashboards rendering the factor breakdown continue to work
    without special-casing the MNQ scorer.
    """
    raw_inputs = {
        "trend_bias": trend_bias,
        "vol_regime": vol_regime,
        "funding_skew": funding_skew,
        "onchain_delta": onchain_delta,
        "sentiment": sentiment,
    }
    factors: list[ConfluenceFactor] = []
    weighted_sum = 0.0
    for name, raw in raw_inputs.items():
        weight = MNQ_WEIGHT_TABLE.get(name, 0.0)
        if weight <= 0.0:
            # Dropped feature — don't surface in factors list since
            # ConfluenceFactor requires weight > 0. Equivalent to
            # "feature not part of this scoring regime".
            continue
        norm = _NORMALIZERS[name](raw)
        factors.append(
            ConfluenceFactor(
                name=name,
                weight=weight,
                raw_value=raw,
                normalized_score=round(norm, 4),
            )
        )
        weighted_sum += norm * weight
    total = round(weighted_sum / _MNQ_TOTAL_WEIGHT * 10.0, 2)
    total = min(total, 10.0)
    return ConfluenceResult(
        factors=factors,
        total_score=total,
        recommended_leverage=_score_to_leverage(total),
        signal=_score_to_signal(total),
    )
