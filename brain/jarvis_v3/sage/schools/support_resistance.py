"""Support / Resistance + price action school.

Detect pivot highs + lows over the last 50 bars; check whether price is
currently at, breaking above, or rejecting a known level.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


def _find_pivots(values: list[float], lookback: int = 3, *, kind: str = "high") -> list[tuple[int, float]]:
    """Return list of (index, value) pairs that are local highs/lows.

    A pivot high is a bar whose value is the max of itself ± lookback bars.
    """
    if kind not in ("high", "low"):
        raise ValueError("kind must be 'high' or 'low'")
    out: list[tuple[int, float]] = []
    for i in range(lookback, len(values) - lookback):
        window = values[i - lookback : i + lookback + 1]
        if kind == "high" and values[i] == max(window):
            out.append((i, values[i]))
        elif kind == "low" and values[i] == min(window):
            out.append((i, values[i]))
    return out


class SupportResistanceSchool(SchoolBase):
    NAME = "support_resistance"
    WEIGHT = 1.1
    KNOWLEDGE = (
        "Support/Resistance: prices tend to bounce at historical levels "
        "where supply and demand have previously turned the market. "
        "Breakouts above resistance OR rejections at support are signals. "
        "Confluence with other levels (round numbers, MAs, prior swing "
        "points) increases reliability."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 30) for pivot detection",
            )

        highs = ctx.highs()
        lows = ctx.lows()
        last_close = float(ctx.bars[-1]["close"])

        pivot_highs = [v for _, v in _find_pivots(highs, kind="high")]
        pivot_lows = [v for _, v in _find_pivots(lows, kind="low")]
        if not pivot_highs or not pivot_lows:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.10,
                aligned_with_entry=False,
                rationale="no clear pivot structure",
            )

        # Closest resistance above + support below
        resistance_above = min((h for h in pivot_highs if h > last_close), default=None)
        support_below = max((l for l in pivot_lows if l < last_close), default=None)

        # Distance to nearest level (as % of price)
        if resistance_above is not None and support_below is not None:
            dist_to_r = (resistance_above - last_close) / last_close
            dist_to_s = (last_close - support_below) / last_close
            at_resistance = dist_to_r < 0.005   # within 50 bps
            at_support = dist_to_s < 0.005
        else:
            dist_to_r = dist_to_s = 0
            at_resistance = at_support = False

        if at_support:
            bias, rationale, conv = (
                Bias.LONG,
                f"price at support ({support_below:.2f}); bounce favored",
                0.65,
            )
        elif at_resistance:
            bias, rationale, conv = (
                Bias.SHORT,
                f"price at resistance ({resistance_above:.2f}); rejection favored",
                0.65,
            )
        elif support_below and resistance_above:
            # Mid-range -> neutral, slight directional bias toward closer level
            if dist_to_r < dist_to_s:
                bias, rationale, conv = Bias.LONG, "closer to resistance -- mild momentum bias", 0.20
            else:
                bias, rationale, conv = Bias.SHORT, "closer to support -- mild fade bias", 0.20
        else:
            bias, rationale, conv = Bias.NEUTRAL, "no nearby level", 0.10

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "resistance_above": resistance_above,
                "support_below": support_below,
                "at_support": at_support,
                "at_resistance": at_resistance,
                "n_pivot_highs": len(pivot_highs),
                "n_pivot_lows": len(pivot_lows),
            },
        )
