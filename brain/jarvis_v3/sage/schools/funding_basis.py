"""Funding / Basis school — enhanced with cross-exchange spread and annualized yield.

Wave-5 #8 + BTC integration 2026-05-01:
  * cross-exchange funding spread → regime quality/convergence signal
  * annualized yield → carry trade attractiveness
  * perp vs spot vs CME futures basis across venues
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
        "magnitude reveals crowded positioning. Cross-exchange spread "
        "reveals regime health. Elevated positive funding = longs paying "
        "shorts = crowded long, contrarian short bias. Negative funding "
        "= shorts paying = crowded short, contrarian long bias. "
        "Wide cross-exchange spread (>3bps) signals regime instability; "
        "tight spread (<0.5bps) signals healthy market. "
        "Annualized yield >20% suggests overheated carry trade. "
        "For CME futures, perp/spot or front/back basis tells similar story."
    )

    HIGH_FUNDING_BPS = 5.0
    EXTREME_FUNDING_BPS = 15.0
    WIDE_CROSS_EXCHANGE_BPS = 3.0
    HIGH_ANNUALIZED_YIELD_PCT = 20.0

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        telemetry = getattr(ctx, "funding", None)
        if not isinstance(telemetry, dict):
            telemetry = {}
        funding = telemetry.get("funding_rate_bps", getattr(ctx, "funding_rate_bps", None))
        basis = telemetry.get("perp_spot_basis_pct", getattr(ctx, "perp_spot_basis_pct", None))
        cross_spread = telemetry.get("cross_exchange_spread_bps")
        ann_yield = telemetry.get("annualized_yield_pct")
        telemetry.get("exchange_rates", {})

        signals: dict = {}
        rationale_parts: list[str] = []

        if funding is None and basis is None:
            return SchoolVerdict(
                school=self.NAME,
                bias=Bias.NEUTRAL,
                conviction=0.0,
                aligned_with_entry=False,
                rationale="no funding/basis telemetry on ctx",
                signals={"missing": ["funding_rate_bps", "perp_spot_basis_pct"]},
            )

        score = 0.0
        if isinstance(funding, (int, float)):
            score -= funding / max(self.HIGH_FUNDING_BPS, 1e-9)
            signals["funding_rate_bps"] = funding
            rationale_parts.append(f"funding={funding}bps")

        if isinstance(basis, (int, float)):
            score -= basis * 5.0
            signals["perp_spot_basis_pct"] = basis
            rationale_parts.append(f"basis={basis}%")

        # Cross-exchange spread: wide spread = regime instability
        if isinstance(cross_spread, (int, float)) and cross_spread > 0:
            signals["cross_exchange_spread_bps"] = cross_spread
            if cross_spread > self.WIDE_CROSS_EXCHANGE_BPS:
                score *= 0.7  # discount confidence due to regime noise
                rationale_parts.append(f"wide cross-ex spread={cross_spread:.1f}bps (risk)")
            else:
                rationale_parts.append(f"tight cross-ex spread={cross_spread:.1f}bps (stable)")

        # Annualized yield: extreme = overheated
        if isinstance(ann_yield, (int, float)):
            signals["annualized_yield_pct"] = ann_yield
            if ann_yield > self.HIGH_ANNUALIZED_YIELD_PCT:
                score *= 1.3  # amplify signal — carry trade extreme
                rationale_parts.append(f"high ann yield={ann_yield:.1f}% (overheated)")

        if score <= -1.0:
            bias, conv = Bias.SHORT, min(0.85, abs(score) * 0.4)
            rationale = "crowded long — " + "; ".join(rationale_parts)
        elif score >= 1.0:
            bias, conv = Bias.LONG, min(0.85, abs(score) * 0.4)
            rationale = "crowded short — " + "; ".join(rationale_parts)
        else:
            bias, conv = Bias.NEUTRAL, 0.20
            rationale = "positioning balanced — " + "; ".join(rationale_parts)

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals=signals,
        )
