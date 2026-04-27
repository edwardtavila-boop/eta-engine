"""Fibonacci retracement / extension analyzer.

Compute key retracement levels (23.6, 38.2, 50, 61.8, 78.6) of the most
recent swing leg. If price is currently AT a high-conviction level (61.8
or 78.6) the school flags a continuation/reversal opportunity.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)

FIB_LEVELS: list[float] = [0.236, 0.382, 0.500, 0.618, 0.786]


class FibonacciSchool(SchoolBase):
    NAME = "fibonacci"
    WEIGHT = 0.9
    KNOWLEDGE = (
        "Fibonacci retracement (golden ratio): pullbacks in trends often "
        "stop at 23.6, 38.2, 50.0, 61.8, 78.6 percent of the prior leg. "
        "61.8 and 78.6 are 'deep' levels with the highest historical "
        "bounce rate. Extensions (127.2, 161.8) project trend targets."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 30) for swing detection",
            )

        # Find the most recent swing high + low in the last 30 bars
        highs = ctx.highs()
        lows = ctx.lows()
        last_close = float(ctx.bars[-1]["close"])

        swing_high = max(highs[-30:])
        swing_low = min(lows[-30:])
        leg = swing_high - swing_low
        if leg <= 0:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="degenerate swing leg",
            )

        # Determine direction of the most recent leg by which extreme is more recent
        idx_high = max(range(n - 30, n), key=lambda i: highs[i])
        idx_low = max(range(n - 30, n), key=lambda i: -lows[i])  # most recent low
        leg_up = idx_high > idx_low

        if leg_up:
            # Up leg: retracement is from swing_high back toward swing_low
            retrace_pct = (swing_high - last_close) / leg if leg else 0
        else:
            retrace_pct = (last_close - swing_low) / leg if leg else 0

        # Closest level
        closest_level = min(FIB_LEVELS, key=lambda lvl: abs(lvl - retrace_pct))
        distance = abs(closest_level - retrace_pct)
        at_level = distance < 0.025  # within 2.5%

        if not at_level:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.10,
                aligned_with_entry=False,
                rationale=f"retracement {retrace_pct*100:.1f}% not near a key level",
                signals={
                    "retrace_pct": retrace_pct,
                    "closest_level": closest_level,
                    "leg_up": leg_up,
                },
            )

        # At a Fib level. Bias = continuation of the prior leg.
        if leg_up:
            bias = Bias.LONG
            rationale = f"price at {closest_level*100:.1f}% retracement of up leg -- continuation favored"
        else:
            bias = Bias.SHORT
            rationale = f"price at {closest_level*100:.1f}% retracement of down leg -- continuation favored"

        # Deep retracements (61.8, 78.6) get higher conviction
        conviction = {
            0.236: 0.35, 0.382: 0.45, 0.500: 0.55,
            0.618: 0.75, 0.786: 0.65,
        }[closest_level]

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conviction,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "retrace_pct": retrace_pct,
                "closest_level": closest_level,
                "leg_up": leg_up,
                "swing_high": swing_high,
                "swing_low": swing_low,
            },
        )
