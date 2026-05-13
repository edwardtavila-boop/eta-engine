"""Cumulative delta + order flow analytics (Tier-1 #3, part 1, 2026-04-27).

Pure-stdlib computations over BBO 1-min bars (already captured by the
``EtaIbkrBbo1mCapture`` task). Produces per-bar:

  * delta              -- buy_volume - sell_volume on this bar
  * cumulative_delta   -- running sum since session start
  * delta_divergence   -- price made HH/LL but cumulative delta did not
  * absorption_score   -- |delta| / |price_change| (high = absorption)

Bots can use these as confluence/filters (e.g. v17 already approves
the entry; only fire if cumulative_delta agrees with the direction).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class FlowBar:
    """One bar of BBO-derived order flow."""

    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    buy_volume: float
    sell_volume: float

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume


@dataclass
class FlowSeries:
    """Computed flow series over a sequence of bars."""

    cumulative_delta: list[float]
    bar_deltas: list[float]
    absorption: list[float]
    divergences: list[bool]


def compute_flow_series(bars: Sequence[FlowBar]) -> FlowSeries:
    """Compute cumulative delta + per-bar absorption + divergences."""
    cd: list[float] = []
    bd: list[float] = []
    ab: list[float] = []
    dv: list[bool] = []

    running = 0.0
    last_high = 0.0
    last_low = float("inf")
    last_cd_at_high = 0.0
    last_cd_at_low = 0.0

    for i, b in enumerate(bars):
        running += b.delta
        cd.append(round(running, 4))
        bd.append(round(b.delta, 4))

        price_change = abs(b.close - b.open) if (b.close != b.open) else 1e-9
        absorption = abs(b.delta) / price_change
        ab.append(round(absorption, 2))

        # Divergence: new high in price but cumulative delta is LOWER
        # than the last cd-at-high; or new low but cd HIGHER than last
        is_new_high = b.high > last_high
        is_new_low = b.low < last_low
        diverge = False
        if i > 0 and ((is_new_high and running < last_cd_at_high) or (is_new_low and running > last_cd_at_low)):
            diverge = True
        if is_new_high:
            last_high = b.high
            last_cd_at_high = running
        if is_new_low:
            last_low = b.low
            last_cd_at_low = running
        dv.append(diverge)

    return FlowSeries(cumulative_delta=cd, bar_deltas=bd, absorption=ab, divergences=dv)


def has_recent_divergence(series: FlowSeries, *, lookback_bars: int = 5) -> bool:
    if not series.divergences:
        return False
    return any(series.divergences[-lookback_bars:])


def average_absorption(series: FlowSeries, *, lookback_bars: int = 20) -> float:
    if not series.absorption:
        return 0.0
    sample = series.absorption[-lookback_bars:]
    return round(sum(sample) / len(sample), 2)


def cumulative_delta_alignment(
    series: FlowSeries,
    *,
    direction: str,
    lookback_bars: int = 10,
) -> float:
    """Return alignment score in [-1.0, +1.0].

    Positive when cumulative delta is increasing during a long trade
    direction (or decreasing during a short). Computed as the sign of
    the last-N-bar slope of cumulative_delta vs. direction.
    """
    if len(series.cumulative_delta) < lookback_bars:
        return 0.0
    sample = series.cumulative_delta[-lookback_bars:]
    slope = sample[-1] - sample[0]
    is_long = direction.lower() in ("long", "buy", "bull")
    sign = 1.0 if (slope > 0) == is_long else -1.0
    # Normalize by total absolute movement so the score is bounded
    total = sum(abs(sample[i] - sample[i - 1]) for i in range(1, len(sample))) or 1.0
    return round(sign * min(1.0, abs(slope) / total), 3)
