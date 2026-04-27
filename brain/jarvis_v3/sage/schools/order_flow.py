"""Order Flow school -- delta + book imbalance proxy.

Uses MarketContext.cumulative_delta and order_book_imbalance when
provided. Without that telemetry, falls back to a low-conviction NEUTRAL
verdict (so the school doesn't push composite without real data).
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class OrderFlowSchool(SchoolBase):
    NAME = "order_flow"
    WEIGHT = 1.0
    KNOWLEDGE = (
        "Order Flow Trading: real-time aggressive buy vs sell volume "
        "(delta), absorption (large orders defended without price move), "
        "bid/ask imbalance. Footprint charts + cumulative delta reveal "
        "where institutions actually transact. Modern professional + prop "
        "trader methodology requiring Level II data."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        delta = ctx.cumulative_delta
        imb = ctx.order_book_imbalance

        if delta is None and imb is None:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="no order-flow telemetry provided -- skipping",
                signals={"missing": ["cumulative_delta", "order_book_imbalance"]},
            )

        score = 0.0
        signals: dict = {}
        if delta is not None:
            signals["cumulative_delta"] = delta
            score += delta * 0.5  # delta is in any units; treat as directional bias
        if imb is not None:
            signals["order_book_imbalance"] = imb
            # imb in [-1, +1]; +1 = all bid volume, -1 = all ask
            score += imb * 1.0

        if score >= 0.30:
            bias, conv = Bias.LONG, min(0.85, abs(score))
            rationale = f"order-flow score +{score:.2f} -- aggressive buying / book imbalance up"
        elif score <= -0.30:
            bias, conv = Bias.SHORT, min(0.85, abs(score))
            rationale = f"order-flow score {score:.2f} -- aggressive selling / book imbalance down"
        else:
            bias, conv = Bias.NEUTRAL, 0.15
            rationale = f"order-flow score {score:.2f} -- balanced book"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals=signals,
        )
