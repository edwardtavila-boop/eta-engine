"""
EVOLUTIONARY TRADING ALGO  //  features.funding_skew
========================================
Contrarian funding-rate edge.
When funding is hot opposite to our bias, we fade the crowd.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.features.base import Feature

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData, FundingRate

# Threshold: rates above 0.05% (8h equivalent) are "hot".
_HOT_THRESHOLD = 0.0005


def cumulative_funding_8h(rates: list[FundingRate]) -> float:
    """Sum of funding rates over the last ~8h window.

    Assumes 1h-spaced rate entries; sums up to the last 8 samples.
    Returns cumulative decimal rate (e.g. 0.0008 = +8 bps).
    """
    if not rates:
        return 0.0
    window = rates[-8:] if len(rates) >= 8 else rates
    return sum(r.rate for r in window)


class FundingSkewFeature(Feature):
    """Funding-rate contrarian feature.

    Expects in `ctx`:
      - `funding_history`: list[FundingRate]
      - `bias`: int — +1 long / -1 short / 0 flat (our intended side)

    Returns 1.0 when 8h cumulative funding is hot (>0.05%) and opposite
    to our bias (longs crowded when we want short, or vice versa).
    """

    name: str = "funding_skew"
    weight: float = 2.0

    def compute(self, bar: BarData, ctx: dict[str, Any]) -> float:
        rates: list[FundingRate] = ctx.get("funding_history", []) or []
        bias: int = int(ctx.get("bias", 0))

        cum = cumulative_funding_8h(rates)
        abs_cum = abs(cum)

        # Strength in [0, 1] based on magnitude vs hot threshold.
        strength = min(1.0, abs_cum / _HOT_THRESHOLD)

        if strength < 1.0 * 0.5:
            # Not hot enough to be useful
            return strength * 0.5

        # Hot. Is it on the right side?
        # Positive funding = longs pay shorts = crowd is long.
        # We want contrarian: bias == -1 (short) + positive funding = good.
        if bias == 0:
            # No directional bias — just reward the signal intensity
            return strength
        if (cum > 0 and bias < 0) or (cum < 0 and bias > 0):
            return strength  # contrarian alignment
        return max(0.0, 1.0 - strength)  # crowded same side as us — bad
