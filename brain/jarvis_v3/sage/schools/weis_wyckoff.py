"""Weis Wyckoff school -- David Weis's modern Wyckoff update.

Focuses on price-volume waves: cumulative volume per price wave (up vs
down) reveals when sellers are exhausted (selling waves shrinking on
declines) or buyers are exhausted (buying waves shrinking on rallies).
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class WeisWyckoffSchool(SchoolBase):
    NAME = "weis_wyckoff"
    WEIGHT = 0.9
    KNOWLEDGE = (
        "David Weis's Wyckoff modernization: track cumulative volume per "
        "directional 'wave' (consecutive bars in one direction). "
        "Decreasing volume on consecutive down waves -> sellers exhausting; "
        "decreasing volume on consecutive up waves -> buyers exhausting. "
        "Pure price + volume, no indicators."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 20:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 20)",
            )
        bars = ctx.bars[-20:]
        # Build "waves": consecutive bars in same direction
        waves: list[tuple[str, float]] = []  # (direction, sum_volume)
        cur_dir = ""
        cur_vol = 0.0
        for i in range(1, len(bars)):
            d = "up" if float(bars[i]["close"]) > float(bars[i - 1]["close"]) else "down"
            if d != cur_dir:
                if cur_dir:
                    waves.append((cur_dir, cur_vol))
                cur_dir = d
                cur_vol = float(bars[i].get("volume", 0))
            else:
                cur_vol += float(bars[i].get("volume", 0))
        if cur_dir:
            waves.append((cur_dir, cur_vol))

        up_vols = [v for d, v in waves if d == "up"]
        dn_vols = [v for d, v in waves if d == "down"]
        if len(up_vols) < 2 or len(dn_vols) < 2:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.10,
                aligned_with_entry=False,
                rationale="insufficient wave count",
                signals={"n_waves": len(waves)},
            )

        # Last 2 wave volumes in each direction
        u1, u2 = up_vols[-2], up_vols[-1]
        d1, d2 = dn_vols[-2], dn_vols[-1]
        sellers_exhausted = d2 < d1 * 0.7
        buyers_exhausted = u2 < u1 * 0.7

        if sellers_exhausted and not buyers_exhausted:
            bias, conv = Bias.LONG, 0.65
            rationale = f"down-wave volume shrinking ({d1:.0f}->{d2:.0f}) -- sellers exhausting"
        elif buyers_exhausted and not sellers_exhausted:
            bias, conv = Bias.SHORT, 0.65
            rationale = f"up-wave volume shrinking ({u1:.0f}->{u2:.0f}) -- buyers exhausting"
        elif u2 > u1 and d2 < d1:
            bias, conv = Bias.LONG, 0.55
            rationale = "up-vol expanding + down-vol contracting -- bullish"
        elif d2 > d1 and u2 < u1:
            bias, conv = Bias.SHORT, 0.55
            rationale = "down-vol expanding + up-vol contracting -- bearish"
        else:
            bias, conv = Bias.NEUTRAL, 0.15
            rationale = "no clear exhaustion / accumulation pattern"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "n_waves": len(waves),
                "u1": u1, "u2": u2,
                "d1": d1, "d2": d2,
                "sellers_exhausted": sellers_exhausted,
                "buyers_exhausted": buyers_exhausted,
            },
        )
