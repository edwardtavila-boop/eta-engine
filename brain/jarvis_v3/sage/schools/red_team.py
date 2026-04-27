"""Adversarial Red-Team school (Wave-5 #14, 2026-04-27).

Deliberately argues the OPPOSITE of the proposed entry. Looks for the
strongest counter-thesis using a small set of known reversal cues:

  * over-extension from MA (mean-reversion candidate)
  * recent failure at S/R (failed breakout)
  * volume divergence (price up + volume down = exhaustion)
  * crowded position (consensus trade often fails)

If Red Team finds no credible counter, conviction is low (good --
trade the proposed side). If Red Team finds STRONG counter, conviction
is high in the OPPOSITE direction.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


def _ema(values: list[float], period: int) -> list[float]:
    if not values or period < 1:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


class RedTeamSchool(SchoolBase):
    NAME = "red_team"
    WEIGHT = 0.8
    KNOWLEDGE = (
        "Adversarial Red-Team school: deliberately argues the OPPOSITE of "
        "the proposed entry. If Red Team finds no credible counter, the "
        "consensus is robust. If Red Team finds STRONG counter (extension, "
        "failure at S/R, volume divergence), conviction in the OPPOSITE "
        "direction stress-tests the trade. Inspired by red-team / "
        "devil's-advocate practices in adversarial review."
    )

    OVERSTRETCH_PCT = 0.015  # 150 bps from EMA-20 = stretched
    DIVERGENCE_LOOKBACK = 5

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        if ctx.n_bars < 25:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({ctx.n_bars} < 25)",
            )

        closes = ctx.closes()
        volumes = ctx.volumes()
        last_close = closes[-1]

        # Counter-thesis 1: stretched from EMA-20 -> mean-reversion candidate
        ema20 = _ema(closes, 20)[-1]
        stretch = (last_close - ema20) / max(ema20, 1e-9)
        overstretched_up = stretch > self.OVERSTRETCH_PCT
        overstretched_dn = stretch < -self.OVERSTRETCH_PCT

        # Counter-thesis 2: volume divergence over last N bars
        # Price up + volume DOWN = bearish divergence; price down + volume DOWN = bullish
        n = self.DIVERGENCE_LOOKBACK
        if n + 1 < len(closes):
            price_change = closes[-1] - closes[-(n + 1)]
            recent_vol_avg = sum(volumes[-n:]) / n
            prior_vol_avg = sum(volumes[-(2 * n):-n]) / n if len(volumes) >= 2 * n else recent_vol_avg
            vol_change = recent_vol_avg - prior_vol_avg
            bearish_div = price_change > 0 and vol_change < -0.1 * abs(prior_vol_avg)
            bullish_div = price_change < 0 and vol_change < -0.1 * abs(prior_vol_avg)
        else:
            bearish_div = bullish_div = False

        entry_side = ctx.side.lower()

        # Synthesize counter-thesis
        if entry_side == "long":
            # Looking for SHORT counter
            if overstretched_up and bearish_div:
                return SchoolVerdict(
                    school=self.NAME, bias=Bias.SHORT, conviction=0.75,
                    aligned_with_entry=False,
                    rationale=(
                        f"strong COUNTER to long: overstretched (+{stretch*100:.1f}% from EMA20) "
                        f"+ bearish vol divergence -- mean-reversion candidate"
                    ),
                    signals={
                        "stretch": stretch, "overstretched_up": overstretched_up,
                        "bearish_div": bearish_div,
                    },
                )
            if overstretched_up:
                return SchoolVerdict(
                    school=self.NAME, bias=Bias.SHORT, conviction=0.45,
                    aligned_with_entry=False,
                    rationale=f"counter to long: stretched +{stretch*100:.1f}% from EMA20",
                    signals={"stretch": stretch, "overstretched_up": True},
                )
            if bearish_div:
                return SchoolVerdict(
                    school=self.NAME, bias=Bias.SHORT, conviction=0.40,
                    aligned_with_entry=False,
                    rationale="counter to long: bearish volume divergence",
                    signals={"bearish_div": True},
                )
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.10,
                aligned_with_entry=True,
                rationale="no credible counter to long entry -- consensus robust",
                signals={"stretch": stretch, "bearish_div": False},
            )
        else:
            # entry_side == "short" -- look for LONG counter
            if overstretched_dn and bullish_div:
                return SchoolVerdict(
                    school=self.NAME, bias=Bias.LONG, conviction=0.75,
                    aligned_with_entry=False,
                    rationale=(
                        f"strong COUNTER to short: overstretched ({stretch*100:.1f}% from EMA20) "
                        f"+ bullish vol divergence -- mean-reversion candidate"
                    ),
                    signals={
                        "stretch": stretch, "overstretched_dn": overstretched_dn,
                        "bullish_div": bullish_div,
                    },
                )
            if overstretched_dn:
                return SchoolVerdict(
                    school=self.NAME, bias=Bias.LONG, conviction=0.45,
                    aligned_with_entry=False,
                    rationale=f"counter to short: stretched {stretch*100:.1f}% below EMA20",
                    signals={"stretch": stretch, "overstretched_dn": True},
                )
            if bullish_div:
                return SchoolVerdict(
                    school=self.NAME, bias=Bias.LONG, conviction=0.40,
                    aligned_with_entry=False,
                    rationale="counter to short: bullish volume divergence",
                    signals={"bullish_div": True},
                )
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.10,
                aligned_with_entry=True,
                rationale="no credible counter to short entry -- consensus robust",
                signals={"stretch": stretch, "bullish_div": False},
            )
