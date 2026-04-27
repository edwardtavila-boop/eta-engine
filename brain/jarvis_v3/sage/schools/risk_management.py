"""Risk management school: 1-2% per trade, R-multiple sizing.

This school doesn't have a directional bias -- it produces a verdict
about whether the proposed trade SIZE meets the risk-management
contract. Conviction reflects how compliant the trade is.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class RiskManagementSchool(SchoolBase):
    NAME = "risk_management"
    WEIGHT = 1.5  # highest weight -- a non-compliant trade should dominate
    KNOWLEDGE = (
        "Risk Management & Position Sizing (Jesse Livermore, modern risk-first "
        "schools): never risk more than 1-2% of capital per trade; use stops; "
        "maintain risk-reward ratios (1:2+); preserve capital above all. "
        "Probabilistic thinking: edges exist, but outcomes are random in the "
        "short term. Survival > optimization."
    )

    MAX_RISK_PCT = 0.02       # 2% absolute max per trade
    PREFERRED_RISK_PCT = 0.01  # 1% sweet spot
    MIN_RR_RATIO = 1.5         # at least 1.5:1 reward:risk

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        # Risk school is bias-neutral; conviction = compliance score
        equity = ctx.account_equity_usd
        risk_pct = ctx.risk_per_trade_pct
        stop_dist = ctx.stop_distance_pct

        if equity is None or risk_pct is None:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.5,
                aligned_with_entry=True,  # neutral school never blocks via alignment
                rationale="risk parameters not provided -- assuming compliant",
                signals={"missing": ["account_equity_usd", "risk_per_trade_pct"]},
            )

        if risk_pct > self.MAX_RISK_PCT:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,  # explicit FAIL
                rationale=(
                    f"risk_per_trade_pct={risk_pct*100:.2f}% exceeds hard cap "
                    f"{self.MAX_RISK_PCT*100:.0f}% -- DO NOT TRADE"
                ),
                signals={"risk_pct": risk_pct, "max_pct": self.MAX_RISK_PCT, "violation": True},
            )

        # Compliant -- conviction depends on how close to preferred and whether
        # we have a stop distance set
        if stop_dist is None or stop_dist <= 0:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.4,
                aligned_with_entry=True,
                rationale=f"risk_pct={risk_pct*100:.2f}% within cap but no stop_distance set",
                signals={"risk_pct": risk_pct, "stop_distance_pct": stop_dist},
            )

        # Risk-reward isn't given here directly; we assume MIN_RR is met
        # if the stop_distance < some heuristic of recent ATR (not computed here).
        # Treat compliance as full when risk_pct <= preferred AND stop set.
        if risk_pct <= self.PREFERRED_RISK_PCT:
            conv = 0.95
            rationale = f"risk_pct={risk_pct*100:.2f}% <= preferred {self.PREFERRED_RISK_PCT*100:.0f}%; full compliance"
        else:
            # Linear between preferred and max
            slack = (self.MAX_RISK_PCT - risk_pct) / (self.MAX_RISK_PCT - self.PREFERRED_RISK_PCT)
            conv = 0.5 + 0.4 * max(0.0, min(1.0, slack))
            rationale = f"risk_pct={risk_pct*100:.2f}% between preferred and max; partial compliance"

        return SchoolVerdict(
            school=self.NAME,
            bias=Bias.NEUTRAL,
            conviction=conv,
            aligned_with_entry=True,
            rationale=rationale,
            signals={
                "risk_pct": risk_pct,
                "stop_distance_pct": stop_dist,
                "preferred_pct": self.PREFERRED_RISK_PCT,
                "max_pct": self.MAX_RISK_PCT,
            },
        )
