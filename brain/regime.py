"""
EVOLUTIONARY TRADING ALGO  //  brain.regime
===============================
Multi-axis regime classifier.  5 axes in, 1 regime out.
Context is king — wrong regime = wrong everything.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class RegimeType(StrEnum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"
    CRISIS = "CRISIS"
    TRANSITION = "TRANSITION"


class RegimeAxes(BaseModel):
    """Five-axis regime description vector."""

    vol: float = Field(ge=0.0, le=1.0, description="Volatility 0=dead, 1=extreme")
    trend: float = Field(ge=-1.0, le=1.0, description="Trend -1=bear, +1=bull")
    liquidity: float = Field(ge=0.0, le=1.0, description="Liquidity 0=dry, 1=deep")
    correlation: float = Field(ge=0.0, le=1.0, description="Cross-asset corr 0=dispersed, 1=lockstep")
    macro: str = Field(description="Macro label: hawkish, dovish, neutral, crisis")


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify_regime(axes: RegimeAxes) -> RegimeType:
    """Decision-tree regime classifier on 5 axes.

    Priority order (first match wins):
        1. CRISIS:     macro=crisis OR (vol>0.85 AND liquidity<0.2)
        2. HIGH_VOL:   vol>0.7 AND correlation>0.7
        3. LOW_VOL:    vol<0.2 AND abs(trend)<0.2
        4. TRENDING:   abs(trend)>0.5 AND vol in [0.2, 0.7]
        5. RANGING:    abs(trend)<0.3 AND vol in [0.2, 0.5]
        6. TRANSITION: everything else
    """
    v, t, liq, corr = axes.vol, axes.trend, axes.liquidity, axes.correlation
    abs_t = abs(t)

    # 1. Crisis
    if axes.macro == "crisis" or (v > 0.85 and liq < 0.2):
        return RegimeType.CRISIS

    # 2. High vol
    if v > 0.7 and corr > 0.7:
        return RegimeType.HIGH_VOL

    # 3. Low vol
    if v < 0.2 and abs_t < 0.2:
        return RegimeType.LOW_VOL

    # 4. Trending
    if abs_t > 0.5 and 0.2 <= v <= 0.7:
        return RegimeType.TRENDING

    # 5. Ranging
    if abs_t < 0.3 and 0.2 <= v <= 0.5:
        return RegimeType.RANGING

    # 6. Default
    return RegimeType.TRANSITION


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def detect_drift(recent_regimes: list[RegimeType], window: int = 20) -> bool:
    """Detect regime drift: current regime differs from mode of last `window`.

    Returns True when the latest regime is NOT the most common regime
    in the trailing window. Signals that adaptation may be needed.
    """
    if len(recent_regimes) < 2:
        return False

    lookback = recent_regimes[-window:]
    counter = Counter(lookback)
    mode_regime = counter.most_common(1)[0][0]
    current = recent_regimes[-1]
    return current != mode_regime
