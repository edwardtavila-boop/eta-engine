"""NEoWave school -- structured Elliott extension (Glenn Neely).

NEoWave's strict rule set requires a full wave-counter with retroactive
relabeling. Without that, this school adds a 'structural certainty'
modifier on top of the Elliott school: when the impulse momentum is
clean (low pullback noise), upgrade conviction; when it's noisy, downgrade.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)


class NEoWaveSchool(SchoolBase):
    NAME = "neowave"
    WEIGHT = 0.6
    KNOWLEDGE = (
        "NEoWave (Glenn Neely): structured Elliott Wave with stricter rules + "
        "objective guidelines, addressing the subjectivity of classic wave "
        "counting. More systematic for forecasting in volatile markets; uses "
        "Fibonacci ratios prominently. Best when wave structure is clean "
        "(low intra-wave noise)."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        n = ctx.n_bars
        if n < 30:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale=f"insufficient bars ({n} < 30)",
            )
        closes = ctx.closes()
        # Path-noise: total absolute return / net return over last 20 bars
        net_move = abs(closes[-1] - closes[-20])
        path_sum = sum(abs(closes[i] - closes[i - 1]) for i in range(n - 19, n))
        if path_sum <= 0:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False, rationale="zero path",
            )
        cleanness = net_move / path_sum  # 1.0 = perfect straight line, 0.0 = pure chop
        last_dir = 1 if closes[-1] > closes[-20] else -1

        if cleanness > 0.4 and last_dir > 0:
            bias, conv = Bias.LONG, min(0.7, 0.3 + cleanness)
            rationale = f"clean impulse up (cleanness={cleanness:.2f})"
        elif cleanness > 0.4 and last_dir < 0:
            bias, conv = Bias.SHORT, min(0.7, 0.3 + cleanness)
            rationale = f"clean impulse down (cleanness={cleanness:.2f})"
        else:
            bias, conv = Bias.NEUTRAL, 0.20
            rationale = f"noisy structure (cleanness={cleanness:.2f}) -- defer"

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={"cleanness": cleanness, "last_dir": last_dir},
        )
