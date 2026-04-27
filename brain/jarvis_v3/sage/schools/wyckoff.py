"""Wyckoff Method analyzer (Richard Wyckoff, 1930s).

Heuristic: detect spring (false breakdown that reverses) and upthrust
(false breakout that reverses) -- the canonical Wyckoff entries. Plus
phase classification (accumulation if range-bound after downtrend,
distribution if range-bound after uptrend, markup/markdown if trending).

The 3 laws (Supply/Demand, Cause/Effect, Effort vs Result) all reduce
to: did volume confirm the price move? VPA school covers this.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class WyckoffSchool(SchoolBase):
    NAME = "wyckoff"
    WEIGHT = 1.3  # high weight -- foundational + actionable
    KNOWLEDGE = (
        "Wyckoff Method (Richard Wyckoff, 1930s): markets cycle through "
        "accumulation (smart money buying), markup (uptrend), distribution "
        "(smart money selling), markdown (downtrend). Three laws: "
        "(1) Supply & Demand drives price, (2) Cause & Effect (cause = "
        "accumulation, effect = trend), (3) Effort vs Result (volume vs "
        "price move). The Composite Man is institutional behavior. "
        "Spring = false breakdown then reversal up (long entry). Upthrust = "
        "false breakout then reversal down (short entry)."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 30) for phase assessment",
                signals={"n_bars": n},
            )

        bars = ctx.bars
        highs = ctx.highs()
        lows = ctx.lows()
        closes = ctx.closes()
        volumes = ctx.volumes()

        # Range over last 20 bars
        range_high = max(highs[-20:])
        range_low = min(lows[-20:])
        last = bars[-1]
        last_close = float(last["close"])
        last_low = float(last["low"])
        last_high = float(last["high"])
        last_vol = float(last.get("volume", 0))
        avg_vol = sum(volumes[-20:]) / 20 if volumes[-20:] else 0

        # Spring: last bar's low pierced range_low BUT closed back inside on volume
        spring = (
            last_low < range_low
            and last_close > range_low
            and last_vol >= avg_vol * 1.2
        )
        # Upthrust: last bar's high pierced range_high BUT closed back inside on volume
        upthrust = (
            last_high > range_high
            and last_close < range_high
            and last_vol >= avg_vol * 1.2
        )

        # Phase classification: trend over last 50 bars
        prior_trend = "neutral"
        if n >= 50:
            ma_first = sum(closes[-50:-25]) / 25
            ma_last = sum(closes[-25:]) / 25
            if ma_last > ma_first * 1.01:
                prior_trend = "up"
            elif ma_last < ma_first * 0.99:
                prior_trend = "down"

        in_range = (range_high - range_low) / max(range_low, 1e-9) < 0.03  # tight 3% range
        if in_range and prior_trend == "down":
            phase = "accumulation"
        elif in_range and prior_trend == "up":
            phase = "distribution"
        elif prior_trend == "up":
            phase = "markup"
        elif prior_trend == "down":
            phase = "markdown"
        else:
            phase = "transitional"

        if spring:
            bias, rationale, conv = Bias.LONG, "spring detected (false breakdown reversed up)", 0.85
        elif upthrust:
            bias, rationale, conv = Bias.SHORT, "upthrust detected (false breakout reversed down)", 0.85
        elif phase == "markup":
            bias, rationale, conv = Bias.LONG, "markup phase -- trend continuation favored", 0.55
        elif phase == "markdown":
            bias, rationale, conv = Bias.SHORT, "markdown phase -- trend continuation favored", 0.55
        elif phase == "accumulation":
            bias, rationale, conv = Bias.LONG, "accumulation phase -- watching for spring", 0.30
        elif phase == "distribution":
            bias, rationale, conv = Bias.SHORT, "distribution phase -- watching for upthrust", 0.30
        else:
            bias, rationale, conv = Bias.NEUTRAL, f"phase={phase} -- no clear setup", 0.15

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "phase": phase,
                "spring": spring,
                "upthrust": upthrust,
                "range_high": range_high,
                "range_low": range_low,
                "in_range": in_range,
                "prior_trend": prior_trend,
            },
        )
