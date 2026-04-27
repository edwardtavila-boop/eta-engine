"""Statistical-significance bootstrap school (Wave-5 #12, 2026-04-27).

Bootstraps the recent return distribution to estimate the probability
that the LAST bar's return is statistically distinguishable from a
random draw. Low p-value (< 0.10) = signal is unlikely to be noise.

This school doesn't have a directional bias by itself -- it returns
LONG/SHORT in the direction of the last move with conviction = 1 - p_value.
"""
from __future__ import annotations

import random

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class StatSignificanceSchool(SchoolBase):
    NAME = "stat_significance"
    WEIGHT = 0.7
    KNOWLEDGE = (
        "Statistical-significance bootstrap school: estimates the p-value "
        "of the last bar's return under the null hypothesis that recent "
        "returns are i.i.d. random draws. Low p (< 0.10) = the move is "
        "unlikely to be noise. Conviction = 1 - p; bias is in the direction "
        "of the last move."
    )

    BOOTSTRAP_N = 200
    LOOKBACK = 50

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        if ctx.n_bars < self.LOOKBACK + 1:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({ctx.n_bars} < {self.LOOKBACK + 1})",
            )

        closes = ctx.closes()
        rets = [
            (closes[i] - closes[i - 1]) / max(closes[i - 1], 1e-9)
            for i in range(len(closes) - self.LOOKBACK, len(closes))
        ]
        last_ret = rets[-1]

        # Bootstrap: resample the historical returns, compute a distribution
        # of single-bar returns; what fraction are AT LEAST as extreme as last_ret?
        rng = random.Random(int(closes[-1] * 10000) % (2**32))  # deterministic per bar
        sample_pool = rets[:-1]
        if len(sample_pool) < 5:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False, rationale="not enough samples",
            )

        more_extreme = sum(
            1 for _ in range(self.BOOTSTRAP_N)
            if abs(rng.choice(sample_pool)) >= abs(last_ret)
        )
        p_value = more_extreme / self.BOOTSTRAP_N

        if last_ret > 0:
            bias = Bias.LONG
        elif last_ret < 0:
            bias = Bias.SHORT
        else:
            bias = Bias.NEUTRAL

        # Conviction = 1 - p, bounded so pure-random returns earn zero conviction
        conviction = max(0.0, 1.0 - p_value)
        # Add a magnitude floor: tiny moves shouldn't earn high conviction
        # even if they're "rare" relative to a flat sample
        if abs(last_ret) < 0.0005:  # 5 bps
            conviction *= 0.3

        rationale = (
            f"last_ret={last_ret*100:.3f}% with bootstrap p={p_value:.3f} "
            f"({self.BOOTSTRAP_N} samples)"
        )

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conviction,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "last_ret": last_ret,
                "p_value": p_value,
                "bootstrap_n": self.BOOTSTRAP_N,
                "lookback": self.LOOKBACK,
            },
        )
