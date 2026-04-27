"""
EVOLUTIONARY TRADING ALGO  //  brain.htf_engine
===================================
Higher-timeframe (HTF) top-down engine.

Produces the inputs that ``features.trend_bias.TrendBiasFeature`` expects:

    daily_ema   : list[float] -- daily EMA series (oldest to newest)
    h4_struct   : "HH_HL" | "LH_LL" | "NEUTRAL"

...plus a composite top-down bias integer in {-1, 0, +1} that downstream
bots can use to gate their 5m/1m entries.

Design contract
---------------
1. Pure stdlib -- no numpy, no pandas. Pydantic for the result shape.
2. Deterministic. Same bars -> same EMA, same structure, same bias.
3. No lookahead. Every computation uses only the bars at or before the
   reference index. EMA is left-to-right seeded; structure uses strict
   left-of-pivot swing confirmation.
4. Hierarchy respected: daily dominates 4H. When they disagree we
   return NEUTRAL rather than defaulting to one side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from eta_engine.core.data_pipeline import BarData

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

Structure = str  # "HH_HL" | "LH_LL" | "NEUTRAL"


class HtfBias(BaseModel):
    """Top-down bias vector produced from Daily + 4H context."""

    daily_ema: list[float] = Field(default_factory=list)
    daily_ema_slope: float = 0.5  # 1.0 = strong up, 0.0 = strong down, 0.5 = flat
    daily_struct: Structure = "NEUTRAL"
    h4_struct: Structure = "NEUTRAL"
    bias: int = Field(0, ge=-1, le=1, description="+1 long, -1 short, 0 flat/neutral")
    agreement: bool = Field(
        default=False,
        description="True when daily and 4H point the same direction.",
    )


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


def compute_ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average. Seed from the first ``period`` SMA.

    Returns a list the same length as ``values``. The first ``period - 1``
    entries are SMA-running (partial seed) so downstream code sees a usable
    series without NaNs. The convention matches what ``features.trend_bias``
    assumes: oldest-first, newest-last.
    """
    if period <= 0:
        raise ValueError(f"period must be positive, got {period}")
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out: list[float] = []
    running_sum = 0.0
    for i, v in enumerate(values):
        if i < period:
            running_sum += v
            out.append(running_sum / (i + 1))
            continue
        prev = out[-1]
        out.append(alpha * v + (1.0 - alpha) * prev)
    return out


def ema_from_bars(bars: list[BarData], period: int) -> list[float]:
    """EMA of bar close prices. Bars assumed oldest-first."""
    closes = [b.close for b in bars]
    return compute_ema(closes, period)


def ema_slope_label(ema: list[float], lookback: int = 20) -> float:
    """Classify the tail of an EMA series as slope in [0, 1].

    1.0 = strong rise, 0.0 = strong fall, 0.5 = flat.
    Threshold: >=2% rise over ``lookback`` points -> 1.0 (clamped).
    """
    if len(ema) < 2:
        return 0.5
    tail = ema[-lookback:] if len(ema) >= lookback else ema
    start, end = tail[0], tail[-1]
    base = (abs(start) + abs(end)) / 2.0 or 1.0
    delta_pct = (end - start) / base
    # Map [-0.02, +0.02] -> [0, 1]
    score = 0.5 + (delta_pct / 0.04)
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Swing detection + structure
# ---------------------------------------------------------------------------


def swing_highs(bars: list[BarData], k: int = 2) -> list[int]:
    """Return indices i where bar[i].high is the max within [i-k, i+k].

    ``k`` = bars-of-confirmation on each side. Endpoints (i<k or i>=n-k)
    are excluded so every swing uses full context and has no lookahead.
    """
    n = len(bars)
    out: list[int] = []
    for i in range(k, n - k):
        h = bars[i].high
        window = bars[i - k : i + k + 1]
        if (
            h == max(b.high for b in window)
            and all(bars[i].high >= b.high for b in window)
            and all(bars[i].high > bars[j].high for j in range(i - k, i))
        ):
            # strict max on the left side -> unambiguous pivot
            out.append(i)
    return out


def swing_lows(bars: list[BarData], k: int = 2) -> list[int]:
    """Return indices i where bar[i].low is the min within [i-k, i+k]."""
    n = len(bars)
    out: list[int] = []
    for i in range(k, n - k):
        window = bars[i - k : i + k + 1]
        if (
            bars[i].low == min(b.low for b in window)
            and all(bars[i].low <= b.low for b in window)
            and all(bars[i].low < bars[j].low for j in range(i - k, i))
        ):
            out.append(i)
    return out


def classify_structure(bars: list[BarData], k: int = 2) -> Structure:
    """Classify market structure from swing-high / swing-low progression.

    Returns:
      "HH_HL"   -- last two swing highs rising AND last two swing lows rising
      "LH_LL"   -- last two swing highs falling AND last two swing lows falling
      "NEUTRAL" -- anything else (includes insufficient swings)
    """
    if len(bars) < 2 * k + 2:
        return "NEUTRAL"
    highs_i = swing_highs(bars, k)
    lows_i = swing_lows(bars, k)
    if len(highs_i) < 2 or len(lows_i) < 2:
        return "NEUTRAL"
    h0, h1 = bars[highs_i[-2]].high, bars[highs_i[-1]].high
    l0, l1 = bars[lows_i[-2]].low, bars[lows_i[-1]].low
    if h1 > h0 and l1 > l0:
        return "HH_HL"
    if h1 < h0 and l1 < l0:
        return "LH_LL"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Top-down bias
# ---------------------------------------------------------------------------

_STRUCT_SIGN: dict[Structure, int] = {"HH_HL": +1, "LH_LL": -1, "NEUTRAL": 0}


def _slope_sign(slope: float) -> int:
    if slope > 0.6:
        return +1
    if slope < 0.4:
        return -1
    return 0


class HtfEngine:
    """Compose daily + 4H context into a top-down bias vector.

    Usage:
        eng = HtfEngine(daily_ema_period=50, h4_swing_k=2, daily_swing_k=2)
        out = eng.top_down(daily_bars, h4_bars)
        ctx = {"daily_ema": out.daily_ema, "h4_struct": out.h4_struct,
               "bias": out.bias}
        # pass ctx to features.trend_bias.TrendBiasFeature
    """

    def __init__(
        self,
        *,
        daily_ema_period: int = 50,
        daily_swing_k: int = 2,
        h4_swing_k: int = 2,
        slope_lookback: int = 20,
    ) -> None:
        if daily_ema_period <= 0:
            raise ValueError("daily_ema_period must be positive")
        if daily_swing_k <= 0 or h4_swing_k <= 0:
            raise ValueError("swing_k must be positive")
        self.daily_ema_period = daily_ema_period
        self.daily_swing_k = daily_swing_k
        self.h4_swing_k = h4_swing_k
        self.slope_lookback = slope_lookback

    def top_down(
        self,
        daily_bars: list[BarData],
        h4_bars: list[BarData],
    ) -> HtfBias:
        """Compute Daily + 4H top-down bias.

        Composition rules (priority top-down):
          1. Require at least ``daily_ema_period`` daily bars. Otherwise NEUTRAL.
          2. Daily bias = sign(ema_slope) when slope is confident, else
             sign(daily_struct). If both are confident they must agree.
          3. 4H bias = sign(h4_struct).
          4. Final bias = daily_bias IF 4H agrees or is neutral; else 0.
          5. ``agreement`` = daily_bias and 4H_bias non-zero + same sign.
        """
        if len(daily_bars) < self.daily_ema_period:
            return HtfBias(bias=0, agreement=False)

        ema = ema_from_bars(daily_bars, self.daily_ema_period)
        slope = ema_slope_label(ema, lookback=self.slope_lookback)
        daily_struct = classify_structure(daily_bars, k=self.daily_swing_k)
        h4_struct = classify_structure(h4_bars, k=self.h4_swing_k)

        slope_sign = _slope_sign(slope)
        daily_struct_sign = _STRUCT_SIGN[daily_struct]
        h4_sign = _STRUCT_SIGN[h4_struct]

        # Daily confluence: slope + structure must not contradict
        if slope_sign != 0 and daily_struct_sign != 0:
            daily_bias = slope_sign if slope_sign == daily_struct_sign else 0
        else:
            daily_bias = slope_sign or daily_struct_sign

        # 4H gate: 4H must agree OR be neutral. Otherwise cancel to 0.
        if daily_bias == 0:
            final_bias = 0
        elif h4_sign == 0 or h4_sign == daily_bias:
            final_bias = daily_bias
        else:
            final_bias = 0

        agreement = daily_bias != 0 and h4_sign != 0 and daily_bias == h4_sign

        return HtfBias(
            daily_ema=ema,
            daily_ema_slope=slope,
            daily_struct=daily_struct,
            h4_struct=h4_struct,
            bias=final_bias,
            agreement=agreement,
        )

    def context_for_trend_bias(
        self,
        daily_bars: list[BarData],
        h4_bars: list[BarData],
    ) -> dict[str, object]:
        """Convenience: produce the dict that TrendBiasFeature.compute expects."""
        out = self.top_down(daily_bars, h4_bars)
        return {
            "daily_ema": out.daily_ema,
            "h4_struct": out.h4_struct,
            "bias": out.bias,
        }
