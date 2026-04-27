"""Gann school -- 1x1 angle + square-of-nine proximity.

Heuristic: compute the 1x1 angle (1 unit price per 1 unit time) from
the swing low; flag whether price is above/below the projected line.
Plus a square-of-nine quick proximity check (price near a key cardinal).
"""
from __future__ import annotations

from math import sqrt

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class GannSchool(SchoolBase):
    NAME = "gann"
    WEIGHT = 0.6
    KNOWLEDGE = (
        "Gann Theory (W.D. Gann, early 1900s): time and price are interrelated. "
        "Gann angles -- 1x1 = 45 degrees, 2x1, 1x2 etc. -- act as dynamic "
        "support/resistance. Square of nine projects price targets via "
        "geometric rotation around a number's square root. 'Past patterns "
        "predict future via geometric and cyclical analysis'."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 30)",
            )
        lows = ctx.lows()
        highs = ctx.highs()
        last_close = float(ctx.bars[-1]["close"])

        # Find swing low + its index in the lookback window
        idx_lo = max(range(n - 30, n), key=lambda i: -lows[i])
        swing_low = lows[idx_lo]
        bars_since = (n - 1) - idx_lo
        # 1x1 angle: 1 price-unit per bar (scaled to instrument's tick size --
        # we approximate using the average bar range)
        bars = ctx.bars[-30:]
        avg_range = sum(float(b["high"]) - float(b["low"]) for b in bars) / 30
        if avg_range <= 0:
            avg_range = max(swing_low * 0.001, 0.001)
        proj_1x1 = swing_low + bars_since * avg_range
        above_1x1 = last_close > proj_1x1

        # Square of nine: nearest cardinal price (sqrt-based grid)
        sqrt_close = sqrt(max(last_close, 1e-9))
        nearest_cardinal_sqrt = round(sqrt_close)
        cardinal_price = nearest_cardinal_sqrt ** 2
        cardinal_dist_pct = abs(last_close - cardinal_price) / max(cardinal_price, 1e-9)
        at_cardinal = cardinal_dist_pct < 0.002  # within 20 bps

        if above_1x1 and not at_cardinal:
            bias, conv = Bias.LONG, 0.45
            rationale = f"close above 1x1 angle from swing low ({swing_low:.2f}); trend support"
        elif not above_1x1 and not at_cardinal:
            bias, conv = Bias.SHORT, 0.45
            rationale = f"close below 1x1 angle from swing low ({swing_low:.2f}); trend break"
        elif at_cardinal:
            bias, conv = Bias.NEUTRAL, 0.40
            rationale = f"price near square-of-nine cardinal {cardinal_price:.2f} -- inflection"
        else:
            bias, conv = Bias.NEUTRAL, 0.15
            rationale = "no clear Gann signal"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "swing_low": swing_low,
                "bars_since_low": bars_since,
                "proj_1x1": proj_1x1,
                "above_1x1": above_1x1,
                "nearest_cardinal": cardinal_price,
                "at_cardinal": at_cardinal,
            },
        )
