"""Dow Theory analyzer (1900s, Charles Dow).

Heuristic: classify the current trend as primary up / primary down /
neutral by looking at the sequence of major highs + lows over the last
50 bars. A primary uptrend = higher highs AND higher lows; downtrend =
lower highs AND lower lows; neutral otherwise.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class DowTheorySchool(SchoolBase):
    NAME = "dow_theory"
    WEIGHT = 1.2  # foundation school -- slightly higher weight
    KNOWLEDGE = (
        "Dow Theory (Charles Dow, late 1800s/early 1900s): markets move in "
        "primary (months/years), secondary (weeks), and minor (days) trends. "
        "Volume confirms trends; averages must confirm each other (industrials "
        "with transports). 'The trend is your friend' until proven otherwise. "
        "Key signals: higher highs + higher lows = primary uptrend; lower highs "
        "+ lower lows = primary downtrend; failure of either = potential reversal."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 20:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 20) for trend assessment",
                signals={"n_bars": n},
            )

        highs = ctx.highs()
        lows = ctx.lows()
        # Use first half vs second half of window to detect higher/lower
        half = n // 2
        h1_high, h2_high = max(highs[:half]), max(highs[half:])
        h1_low,  h2_low  = min(lows[:half]),  min(lows[half:])

        higher_high = h2_high > h1_high
        higher_low  = h2_low  > h1_low
        lower_high  = h2_high < h1_high
        lower_low   = h2_low  < h1_low

        if higher_high and higher_low:
            bias = Bias.LONG
            rationale = "higher highs + higher lows -> primary uptrend"
            conviction = 0.75
        elif lower_high and lower_low:
            bias = Bias.SHORT
            rationale = "lower highs + lower lows -> primary downtrend"
            conviction = 0.75
        elif higher_high or higher_low:
            bias = Bias.LONG
            rationale = "partial uptrend confirmation (only one of HH/HL)"
            conviction = 0.40
        elif lower_high or lower_low:
            bias = Bias.SHORT
            rationale = "partial downtrend confirmation (only one of LH/LL)"
            conviction = 0.40
        else:
            bias = Bias.NEUTRAL
            rationale = "no clear trend (mixed highs/lows)"
            conviction = 0.15

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conviction,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "h1_high": h1_high, "h2_high": h2_high,
                "h1_low":  h1_low,  "h2_low":  h2_low,
                "higher_high": higher_high, "higher_low": higher_low,
                "lower_high":  lower_high,  "lower_low":  lower_low,
            },
        )
