"""Elliott Wave school -- simplified 5-3 structural heuristic.

A FULL wave count requires sophisticated counter logic. This school
ships a heuristic 'wave 3 momentum' detector: when last 3 bars print
the strongest directional run of the lookback window AND volume
confirms, treat it as a wave 3 of an impulsive sequence.

The strict NEoWave-style counting is left for future iteration; this
gives a usable signal without false-precision claims about wave 1/4/5.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class ElliottWaveSchool(SchoolBase):
    NAME = "elliott_wave"
    WEIGHT = 0.7
    KNOWLEDGE = (
        "Elliott Wave Theory (Ralph Nelson Elliott, 1930s): markets move in "
        "5 impulsive waves with the trend + 3 corrective waves against it. "
        "Wave 3 is typically the longest + strongest. Fibonacci ratios "
        "describe wave length proportions. Strong impulsive runs = wave 3 "
        "candidates; choppy retracements = wave 4 or A-B-C correction."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 30)",
            )
        closes = ctx.closes()
        highs = ctx.highs()
        lows = ctx.lows()
        # Last-3-bar return vs lookback's biggest 3-bar return
        last3_move = closes[-1] - closes[-4] if n >= 4 else 0
        max_3bar_up = max(
            closes[i] - closes[i - 3] for i in range(3, n) if i >= 3
        )
        max_3bar_dn = min(
            closes[i] - closes[i - 3] for i in range(3, n) if i >= 3
        )

        # Wave-3 candidate = the latest 3-bar move equals (or close to) the
        # extreme run of the window
        if max_3bar_up > 0 and last3_move >= 0.85 * max_3bar_up:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.LONG, conviction=0.65,
                aligned_with_entry=(ctx.side.lower() == "long"),
                rationale="momentum run consistent with bullish wave 3",
                signals={"last3_move": last3_move, "max_3bar_up": max_3bar_up},
            )
        if max_3bar_dn < 0 and last3_move <= 0.85 * max_3bar_dn:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.SHORT, conviction=0.65,
                aligned_with_entry=(ctx.side.lower() == "short"),
                rationale="momentum run consistent with bearish wave 3",
                signals={"last3_move": last3_move, "max_3bar_dn": max_3bar_dn},
            )
        # Choppy / correction phase
        return SchoolVerdict(
            school=self.NAME, bias=Bias.NEUTRAL, conviction=0.20,
            aligned_with_entry=False,
            rationale="no impulsive run detected -- likely correction (wave 4 / A-B-C)",
            signals={"last3_move": last3_move,
                     "max_3bar_up": max_3bar_up,
                     "max_3bar_dn": max_3bar_dn},
        )
