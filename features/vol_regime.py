"""
EVOLUTIONARY TRADING ALGO  //  features.vol_regime
======================================
ATR percentile sweet-spot detector.
We want live vol but not chaos — 30-70th pctile is the kill zone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.features.base import Feature

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData


def atr_percentile(atrs: list[float], current: float) -> float:
    """Percentile rank of `current` within `atrs` history.

    Returns fraction in [0, 1]. Empty history → 0.5.
    """
    if not atrs:
        return 0.5
    below = sum(1 for a in atrs if a < current)
    return below / len(atrs)


class VolRegimeFeature(Feature):
    """Volatility regime feature.

    Expects in `ctx`:
      - `atr_history`: list[float] — recent ATR readings
      - `atr_current`: float — current ATR value

    Returns 1.0 when current ATR sits in the 30-70th percentile,
    degrades linearly toward 0 at the extremes (dead or chaotic).
    """

    name: str = "vol_regime"
    weight: float = 2.0

    def compute(self, bar: BarData, ctx: dict[str, Any]) -> float:
        atrs: list[float] = ctx.get("atr_history", []) or []
        current: float = float(ctx.get("atr_current", 0.0))

        if current <= 0 or not atrs:
            return 0.0

        pct = atr_percentile(atrs, current)

        # Sweet spot 0.30-0.70 → score 1.0
        if 0.30 <= pct <= 0.70:
            return 1.0

        # Below 0.30: degrade linearly to 0 at pct=0
        if pct < 0.30:
            return max(0.0, pct / 0.30)

        # Above 0.70: degrade linearly to 0 at pct=1.0
        return max(0.0, (1.0 - pct) / 0.30)
