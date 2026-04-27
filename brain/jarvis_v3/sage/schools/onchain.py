"""On-chain school (Wave-5 #9, 2026-04-27).

SCAFFOLD: Glassnode/Coinmetrics-style on-chain metrics for BTC + ETH.
Returns NEUTRAL when ``ctx.onchain`` is absent. When supplied:

    ctx.onchain = {
        "sopr": float,           # Spent Output Profit Ratio (>1 = profit-taking)
        "mvrv": float,           # Market Value to Realized Value
        "nupl": float,           # Net Unrealized Profit/Loss
        "exchange_netflow": float,  # negative = outflow = bullish accumulation
        "dormancy": float,       # higher = older coins moving = bearish
    }
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class OnChainSchool(SchoolBase):
    NAME = "onchain"
    WEIGHT = 1.0
    INSTRUMENTS = frozenset({"crypto"})
    KNOWLEDGE = (
        "On-chain school (BTC/ETH only): SOPR (>1 = profit-taking), MVRV "
        "(market vs realized value -- >2.5 historically a top), NUPL "
        "(net unrealized P&L -- euphoria zone), exchange netflow "
        "(outflow = accumulation), dormancy (old coins moving = "
        "supply unlock risk). Slow-moving but strategically reliable."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        onchain = getattr(ctx, "onchain", None)
        if not onchain or not isinstance(onchain, dict):
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="no on-chain telemetry on ctx -- school skipped",
                signals={"missing": ["ctx.onchain"]},
            )

        score = 0.0
        sopr = onchain.get("sopr")
        mvrv = onchain.get("mvrv")
        nupl = onchain.get("nupl")
        netflow = onchain.get("exchange_netflow")
        dormancy = onchain.get("dormancy")

        if isinstance(sopr, (int, float)):
            score += (1.0 - sopr) * 0.3  # SOPR > 1 = profit-taking = mild bearish
        if isinstance(mvrv, (int, float)):
            if mvrv > 2.5:
                score -= 0.4  # historical top zone
            elif mvrv < 1.0:
                score += 0.3  # historical bottom zone
        if isinstance(nupl, (int, float)):
            if nupl > 0.7:
                score -= 0.3  # euphoria
            elif nupl < 0.1:
                score += 0.2  # capitulation
        if isinstance(netflow, (int, float)):
            score += -netflow * 0.001  # negative netflow (outflow) = bullish
        if isinstance(dormancy, (int, float)) and dormancy > 0.5:
            score -= dormancy * 0.2  # old coins moving = supply pressure

        if score >= 0.30:
            bias, conv = Bias.LONG, min(0.75, abs(score))
            rationale = f"on-chain net bullish (score={score:.2f})"
        elif score <= -0.30:
            bias, conv = Bias.SHORT, min(0.75, abs(score))
            rationale = f"on-chain net bearish (score={score:.2f})"
        else:
            bias, conv = Bias.NEUTRAL, 0.15
            rationale = f"on-chain balanced (score={score:.2f})"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME, bias=bias, conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={**onchain, "score": score},
        )
