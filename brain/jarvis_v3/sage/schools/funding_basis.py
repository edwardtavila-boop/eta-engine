"""Funding / Basis school (Wave-5 #8, 2026-04-27).

SCAFFOLD: returns NEUTRAL when funding_rate / basis fields are absent.
When provided on MarketContext (via getattr ``ctx.funding`` or
``ctx.basis``), produces a positioning-based verdict:

  * elevated positive funding rate = longs paying = crowded long
    -> contrarian SHORT bias
  * negative funding = shorts paying = crowded short
    -> contrarian LONG bias
  * basis (perp - spot) divergence indicates same dynamic
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class FundingBasisSchool(SchoolBase):
    NAME = "funding_basis"
    WEIGHT = 0.9
    INSTRUMENTS = frozenset({"crypto", "futures"})
    KNOWLEDGE = (
        "Funding / Basis school: for crypto perps, funding rate sign + "
        "magnitude reveals crowded positioning. Elevated positive funding "
        "= longs paying shorts = crowded long, contrarian short bias. "
        "For CME futures, perp/spot or front/back basis tells similar "
        "story. Mean-reverting positioning indicator."
    )

    HIGH_FUNDING_BPS = 5.0   # 5 bps per 8h is elevated
    EXTREME_FUNDING_BPS = 15.0

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        funding = getattr(ctx, "funding_rate_bps", None)  # bps per period
        basis = getattr(ctx, "perp_spot_basis_pct", None)

        if funding is None and basis is None:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="no funding/basis telemetry on ctx",
                signals={"missing": ["funding_rate_bps", "perp_spot_basis_pct"]},
            )

        # Combine funding + basis into a single positioning score
        score = 0.0
        if isinstance(funding, (int, float)):
            score -= funding / max(self.HIGH_FUNDING_BPS, 1e-9)  # contrarian
        if isinstance(basis, (int, float)):
            score -= basis * 5.0  # basis in pct; 0.5% premium -> -2.5

        if score <= -1.0:
            bias, conv = Bias.SHORT, min(0.75, abs(score) * 0.4)
            rationale = (
                f"crowded long positioning (funding={funding}bps, "
                f"basis={basis}%) -- contrarian short"
            )
        elif score >= 1.0:
            bias, conv = Bias.LONG, min(0.75, abs(score) * 0.4)
            rationale = (
                f"crowded short positioning (funding={funding}bps, "
                f"basis={basis}%) -- contrarian long"
            )
        else:
            bias, conv = Bias.NEUTRAL, 0.20
            rationale = "positioning balanced"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME, bias=bias, conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={"funding_rate_bps": funding, "perp_spot_basis_pct": basis,
                     "positioning_score": score},
        )
