"""Seasonality / calendar-effects school (Wave-5 #10, 2026-04-27).

Captures deterministic, time-based effects:
  * time-of-day  -- e.g. NY open momentum, lunch-time chop
  * day-of-week  -- e.g. Monday gap-fill, Friday risk-off
  * monthly OPEX -- 3rd Friday of the month
  * end-of-quarter rebalance flows

Heuristic: lookup the current bar's timestamp against a static seasonal
edge table. Returns LONG/SHORT/NEUTRAL with conviction proportional to
the historical edge magnitude.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from eta_engine.brain.jarvis_v3.sage.base import (
    Bias,
    MarketContext,
    SchoolBase,
    SchoolVerdict,
)

#: Hour-of-day (ET) -> (bias, conviction). Rough heuristic; refine from
#: realized data once edge tracker has 3+ months of bars.
HOURLY_EDGE_ET: dict[int, tuple[Bias, float]] = {
    9:  (Bias.LONG,    0.45),  # 09:30 NY open momentum
    10: (Bias.LONG,    0.30),
    11: (Bias.NEUTRAL, 0.10),
    12: (Bias.NEUTRAL, 0.20),  # lunch-time chop
    13: (Bias.NEUTRAL, 0.20),
    14: (Bias.LONG,    0.25),  # afternoon resumption (slight bullish bias historically)
    15: (Bias.NEUTRAL, 0.15),
    16: (Bias.NEUTRAL, 0.10),  # close
}

#: Day-of-week (Mon=0) -> (bias, conviction)
WEEKDAY_EDGE: dict[int, tuple[Bias, float]] = {
    0: (Bias.LONG,    0.20),  # Monday gap-fill bias
    1: (Bias.NEUTRAL, 0.10),
    2: (Bias.NEUTRAL, 0.10),
    3: (Bias.NEUTRAL, 0.10),
    4: (Bias.SHORT,   0.15),  # Friday risk-off bias (mild)
}


def _bar_timestamp_utc(bar: dict[str, Any]) -> datetime | None:
    ts = bar.get("ts") or bar.get("timestamp") or bar.get("time")
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=UTC)
    if isinstance(ts, str):
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


class SeasonalitySchool(SchoolBase):
    NAME = "seasonality"
    WEIGHT = 0.7
    KNOWLEDGE = (
        "Seasonality / calendar-effect school: prices have deterministic "
        "time-based biases. NY open often momentum up; lunch is chop; "
        "Monday gap-fill bias; Friday risk-off; monthly OPEX day "
        "(3rd Friday) brings pinning. Cheap, deterministic, often "
        "overlooked by pure technical schools."
    )

    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        if ctx.n_bars == 0:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False, rationale="no bars",
            )
        last_bar = ctx.bars[-1]
        ts = _bar_timestamp_utc(last_bar)
        if ts is None:
            return SchoolVerdict(
                school=self.NAME, bias=Bias.NEUTRAL, conviction=0.0,
                aligned_with_entry=False,
                rationale="no parseable timestamp on last bar",
            )

        # Convert UTC -> ET (UTC-5 standard, UTC-4 DST). Use UTC-5 for
        # simplicity; intraday traders should override via session_phase.
        et_hour = (ts.hour - 5) % 24
        weekday = ts.weekday()  # Mon=0, Sun=6

        hour_bias, hour_conv = HOURLY_EDGE_ET.get(et_hour, (Bias.NEUTRAL, 0.05))
        wd_bias, wd_conv = WEEKDAY_EDGE.get(weekday, (Bias.NEUTRAL, 0.05))

        # Weekend (Sat/Sun) -> only crypto trades; signal mild
        is_weekend = weekday >= 5

        # 3rd Friday of month -> OPEX day for many equity options
        is_opex = weekday == 4 and 15 <= ts.day <= 21

        # Combine: if both biases agree, sum convictions; else pick stronger
        if hour_bias == wd_bias and hour_bias != Bias.NEUTRAL:
            bias = hour_bias
            conv = min(0.7, hour_conv + wd_conv)
            rationale = f"hour ({et_hour:02d}:00 ET) + weekday agree -> {bias.value}"
        elif hour_conv >= wd_conv and hour_conv > 0.10:
            bias = hour_bias
            conv = hour_conv
            rationale = f"hour ({et_hour:02d}:00 ET) bias dominates"
        elif wd_conv > 0.10:
            bias = wd_bias
            conv = wd_conv
            rationale = f"weekday ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][weekday]}) bias dominates"
        else:
            bias = Bias.NEUTRAL
            conv = 0.05
            rationale = f"no notable seasonal bias at {et_hour:02d}:00 ET / {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][weekday]}"

        if is_opex:
            rationale += " | OPEX day (pin risk)"
            conv = max(conv * 0.7, 0.10)  # OPEX adds pin uncertainty
        if is_weekend:
            conv *= 0.5  # downgrade conviction on weekends

        entry_bias = Bias.LONG if ctx.side.lower() == "long" else Bias.SHORT
        return SchoolVerdict(
            school=self.NAME,
            bias=bias,
            conviction=conv,
            aligned_with_entry=(bias == entry_bias),
            rationale=rationale,
            signals={
                "et_hour": et_hour,
                "weekday": weekday,
                "is_weekend": is_weekend,
                "is_opex_day": is_opex,
                "hour_conv": hour_conv,
                "wd_conv": wd_conv,
            },
        )
