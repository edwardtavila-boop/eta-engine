"""Volatility regime school (Wave-5 #11, 2026-04-27).

Detects vol expansion vs contraction. Bias is NEUTRAL (vol doesn't
have a direction); conviction reflects how much the current regime
favors size-tightening vs status-quo.

Pairs naturally with the regime detector (sage.regime) -- vol
expansion + low directional strength = VOLATILE regime; vol contraction
= QUIET regime; vol expansion + high directional strength = TRENDING.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


class VolatilityRegimeSchool(SchoolBase):
    NAME = "volatility_regime"
    WEIGHT = 0.9
    KNOWLEDGE = (
        "Volatility regime school: realized vol expansion vs contraction. "
        "Current 5-bar realized return-stddev / 50-bar realized return-stddev. "
        ">1.5 = vol expanding (caution / wider stops); <0.6 = vol "
        "contracting (potential breakout setup). NEUTRAL bias since vol "
        "has no direction; high conviction = strong regime call."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        if ctx.n_bars < 55:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=True,  # neutral school doesn't block
                rationale=f"insufficient bars ({ctx.n_bars} < 55)",
            )

        closes = ctx.closes()
        # Compute simple returns
        rets = [
            (closes[i] - closes[i - 1]) / max(closes[i - 1], 1e-9)
            for i in range(1, len(closes))
        ]
        recent_vol = _stddev(rets[-5:])
        baseline_vol = _stddev(rets[-50:])

        if baseline_vol <= 0:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=True, rationale="zero baseline vol",
            )

        vol_ratio = recent_vol / baseline_vol

        if vol_ratio >= 1.5:
            conv = min(0.85, 0.40 + (vol_ratio - 1.5) * 0.3)
            rationale = (
                f"vol expanding sharply (ratio={vol_ratio:.2f}) -- "
                f"WIDER stops + smaller size warranted"
            )
            regime = "expanding"
        elif vol_ratio <= 0.6:
            conv = min(0.75, 0.30 + (0.6 - vol_ratio) * 0.5)
            rationale = (
                f"vol contracting (ratio={vol_ratio:.2f}) -- "
                f"squeeze setup; breakout pending"
            )
            regime = "contracting"
        else:
            conv = 0.20
            rationale = f"vol normal (ratio={vol_ratio:.2f}) -- regime stable"
            regime = "stable"

        return SchoolVerdict(
            school=self.NAME,
            bias=Bias.NEUTRAL,
            conviction=conv,
            aligned_with_entry=True,
            rationale=rationale,
            signals={
                "vol_ratio": vol_ratio,
                "recent_vol": recent_vol,
                "baseline_vol": baseline_vol,
                "regime": regime,
            },
        )
