"""
EVOLUTIONARY TRADING ALGO  //  features.trend_bias
======================================
HTF trend alignment: daily EMA slope + 4H market structure.
1.0 when aligned with our bias, 0.0 when opposed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.features.base import Feature

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData


def ema_slope_score(values: list[float]) -> float:
    """Slope score from an EMA series.

    Returns 1.0 for strong monotonic rise, 0.0 for strong fall,
    0.5 for flat. Looks at first/last delta normalized by mean level.
    """
    if not values or len(values) < 2:
        return 0.5
    start, end = values[0], values[-1]
    mean_abs = (abs(start) + abs(end)) / 2.0 or 1.0
    delta_pct = (end - start) / mean_abs
    # Map [-0.02, +0.02] → [0, 1]
    score = 0.5 + (delta_pct / 0.04)
    return max(0.0, min(1.0, score))


class TrendBiasFeature(Feature):
    """Higher-timeframe trend alignment feature.

    Expects in `ctx`:
      - `daily_ema`: list[float] — recent daily EMA values (oldest→newest)
      - `h4_struct`: str — one of "HH_HL" (bull), "LH_LL" (bear), "NEUTRAL"
      - `bias`: optional int, +1 long / -1 short / 0 flat (our intended direction)
    """

    name: str = "trend_bias"
    weight: float = 3.0

    _STRUCT_SCORES: dict[str, float] = {
        "HH_HL": 1.0,
        "NEUTRAL": 0.5,
        "LH_LL": 0.0,
    }

    def compute(self, bar: BarData, ctx: dict[str, Any]) -> float:
        ema_values: list[float] = ctx.get("daily_ema", []) or []
        struct: str = ctx.get("h4_struct", "NEUTRAL")
        bias: int = int(ctx.get("bias", 0))

        slope = ema_slope_score(ema_values)
        struct_score = self._STRUCT_SCORES.get(struct, 0.5)

        aligned = 0.6 * slope + 0.4 * struct_score

        if bias > 0:
            return aligned  # long bias: high slope = good
        if bias < 0:
            return 1.0 - aligned  # short bias: invert

        # Flat bias: use absolute strength away from 0.5
        return abs(aligned - 0.5) * 2.0
