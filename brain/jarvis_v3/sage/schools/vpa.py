"""Volume Price Analysis (VPA) school.

Effort vs Result: the last bar's volume vs its price move. High volume +
strong move = effort matches result (continuation favored). High volume
+ weak move = effort without result (reversal favored). Low volume on a
move = lack of conviction.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class VPASchool(SchoolBase):
    NAME = "vpa"
    WEIGHT = 1.0
    KNOWLEDGE = (
        "Volume Price Analysis (VPA) / Effort vs Result: volume is the "
        "EFFORT, price move is the RESULT. Effort matches result -> trend "
        "continuation. Effort without result (high vol, small move) -> "
        "absorption / reversal. Low effort + result -> low conviction."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 20:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 20)",
            )
        bars = ctx.bars
        last = bars[-1]
        last_open = float(last["open"])
        last_close = float(last["close"])
        last_vol = float(last.get("volume", 0))

        # Average TRUE volume over prior 20 bars
        avg_vol = sum(float(b.get("volume", 0)) for b in bars[-21:-1]) / 20
        avg_range = sum(float(b["high"]) - float(b["low"]) for b in bars[-21:-1]) / 20
        last_range = float(last["high"]) - float(last["low"])
        last_body = abs(last_close - last_open)

        if avg_vol <= 0 or avg_range <= 0:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="zero baseline volume/range",
            )

        vol_ratio = last_vol / avg_vol
        body_ratio = last_body / avg_range
        wick_ratio = (last_range - last_body) / last_range if last_range > 0 else 0
        is_up_bar = last_close > last_open

        # CASES
        if vol_ratio >= 1.5 and body_ratio >= 1.0:
            # High effort + matching result = continuation
            bias = Bias.LONG if is_up_bar else Bias.SHORT
            rationale = f"high vol ({vol_ratio:.1f}x) + strong move -> continuation"
            conv = 0.75
        elif vol_ratio >= 1.5 and wick_ratio > 0.5:
            # High effort + tiny body + big wick = absorption / reversal
            bias = Bias.SHORT if is_up_bar else Bias.LONG  # fade
            rationale = f"high vol ({vol_ratio:.1f}x) + heavy wick -> absorption / reversal"
            conv = 0.65
        elif vol_ratio < 0.7 and body_ratio >= 1.0:
            # Low vol + strong move = no conviction (counter-trend trap risk)
            bias = Bias.NEUTRAL
            rationale = f"low vol ({vol_ratio:.1f}x) + strong move -> low-conviction; suspect"
            conv = 0.20
        elif vol_ratio >= 1.0:
            bias = Bias.LONG if is_up_bar else Bias.SHORT
            rationale = f"normal vol ({vol_ratio:.1f}x) + directional bar"
            conv = 0.40
        else:
            bias = Bias.NEUTRAL
            rationale = f"low vol + small move (vol_ratio={vol_ratio:.1f})"
            conv = 0.15

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "vol_ratio": vol_ratio,
                "body_ratio": body_ratio,
                "wick_ratio": wick_ratio,
                "is_up_bar": is_up_bar,
            },
        )
