"""Options / Greeks school (Wave-5 #7, 2026-04-27).

SCAFFOLD: returns NEUTRAL with conviction=0 unless the bot supplies
options telemetry in MarketContext via attached fields:

    ctx.options = {
        "dealer_gamma_exposure": float,    # GEX in $millions
        "vol_skew": float,                 # 25-delta put/call IV diff
        "max_pain": float,                 # strike with most pain
        "spot": float,                     # current underlying
        "0dte_squeeze_score": float,       # 0..1
    }

Until ctx.options is wired (typically via a separate options-data
provider), this school is a no-op. Integration shape:

    class MarketContext:
        ...
        options: dict[str, Any] | None = None

The KNOWLEDGE block + verdict signature stay stable so consumers can
opt-in incrementally.
"""
from __future__ import annotations

from typing import Any

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class OptionsGreeksSchool(SchoolBase):
    NAME = "options_greeks"
    WEIGHT = 1.1
    INSTRUMENTS = frozenset({"futures", "equity", "crypto"})  # has listed options
    KNOWLEDGE = (
        "Options / Greeks school: dealer gamma exposure (GEX), vol skew, "
        "max pain, 0DTE squeeze dynamics. For instruments with listed "
        "options (BTC, ETH, MNQ, SPY): positive dealer gamma = market "
        "makers SUPPRESS volatility; negative gamma = market makers "
        "AMPLIFY moves. 0DTE squeeze = unhedged short gamma forces "
        "directional flow late in the day."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        # Look for options telemetry on the context (attribute or dict-key).
        options = getattr(ctx, "options", None)
        if not options or not isinstance(options, dict):
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="no options telemetry on ctx -- school skipped",
                signals={"missing": ["ctx.options"]},
            )

        gex = options.get("dealer_gamma_exposure")
        skew = options.get("vol_skew")
        squeeze = options.get("0dte_squeeze_score") or options.get("squeeze_score")

        score = 0.0
        signals: dict[str, Any] = dict(options)

        if isinstance(gex, (int, float)):
            # Negative GEX = MMs amplify -> volatility regime; bias from skew
            score += -gex * 0.001  # heuristic scale; tune from real data
        if isinstance(skew, (int, float)):
            # Positive put-call skew = put demand = bearish positioning
            score -= skew * 0.5
        if isinstance(squeeze, (int, float)) and squeeze > 0.7:
            # High squeeze score amplifies the directional read
            score *= 1.5

        if score >= 0.30:
            bias, conv = Bias.LONG, min(0.80, abs(score))
            rationale = f"options telemetry net bullish (score={score:.2f})"
        elif score <= -0.30:
            bias, conv = Bias.SHORT, min(0.80, abs(score))
            rationale = f"options telemetry net bearish (score={score:.2f})"
        else:
            bias, conv = Bias.NEUTRAL, 0.20
            rationale = f"options telemetry balanced (score={score:.2f})"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME, bias=bias, conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale, signals=signals,
        )
