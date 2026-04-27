"""Trend following school: EMA stack + ADX-equivalent slope.

Heuristic: compute fast (20) and slow (50) EMAs over closes; bias is
LONG when fast > slow AND fast slope > 0; SHORT when fast < slow AND
fast slope < 0; else NEUTRAL. ADX-equivalent = absolute slope * 100.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


def _ema(values: list[float], period: int) -> list[float]:
    """Standard EMA with alpha = 2/(period+1)."""
    if not values or period < 1:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


class TrendFollowingSchool(SchoolBase):
    NAME = "trend_following"
    WEIGHT = 1.2
    KNOWLEDGE = (
        "Trend Following: 'the trend is your friend until proven otherwise'. "
        "Identify and ride established trends (higher highs/lows in uptrends, "
        "lower highs/lows in downtrends) using moving averages, trendlines, "
        "or ADX. Let winners run; cut losers short. Avoid counter-trend bets."
    )

    FAST_PERIOD = 20
    SLOW_PERIOD = 50
    SLOPE_LOOKBACK = 5

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < self.SLOW_PERIOD + self.SLOPE_LOOKBACK:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < {self.SLOW_PERIOD + self.SLOPE_LOOKBACK})",
            )

        closes = ctx.closes()
        fast_ema = _ema(closes, self.FAST_PERIOD)
        slow_ema = _ema(closes, self.SLOW_PERIOD)
        last_fast = fast_ema[-1]
        last_slow = slow_ema[-1]
        # Slope = (fast_now - fast_lookback_ago) / fast_lookback_ago
        prev_fast = fast_ema[-(self.SLOPE_LOOKBACK + 1)]
        slope = (last_fast - prev_fast) / max(prev_fast, 1e-9)
        # ADX-equivalent: absolute slope scaled
        adx_proxy = abs(slope) * 100.0

        fast_above_slow = last_fast > last_slow
        rising = slope > 0
        falling = slope < 0

        if fast_above_slow and rising:
            bias = Bias.LONG
            rationale = f"fast EMA above slow + rising (slope={slope*100:.2f}%)"
            conv = min(0.85, 0.40 + adx_proxy * 0.5)
        elif (not fast_above_slow) and falling:
            bias = Bias.SHORT
            rationale = f"fast EMA below slow + falling (slope={slope*100:.2f}%)"
            conv = min(0.85, 0.40 + adx_proxy * 0.5)
        elif fast_above_slow:
            bias = Bias.LONG
            rationale = f"EMA stack bullish but slope flat (slope={slope*100:.2f}%)"
            conv = 0.30
        elif not fast_above_slow:
            bias = Bias.SHORT
            rationale = f"EMA stack bearish but slope flat (slope={slope*100:.2f}%)"
            conv = 0.30
        else:
            bias = Bias.NEUTRAL
            rationale = "EMAs flat / unclear"
            conv = 0.15

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "fast_ema": last_fast,
                "slow_ema": last_slow,
                "fast_above_slow": fast_above_slow,
                "slope": slope,
                "adx_proxy": adx_proxy,
            },
        )
