"""EVOLUTIONARY TRADING ALGO  //  strategies.smc_primitives.

Pure bar-level SMC/ICT detectors. No I/O. No hidden state. Each
detector takes an ordered list of :class:`Bar` (oldest -> newest) and
returns either a dataclass describing what was detected, or ``None``.

Primitives provided
-------------------
  * :func:`find_equal_levels`       -- equal highs / equal lows clusters.
  * :func:`detect_liquidity_sweep`  -- wick through an equal level +
    body closes back inside.
  * :func:`detect_displacement`     -- body >= ``body_mult`` * the median
    body of the lookback window.
  * :func:`detect_fvg`              -- 3-bar fair-value-gap
    (bar[-3].high < bar[-1].low  => bull FVG, and vice versa).
  * :func:`detect_break_of_structure` -- price takes out the last swing
    high / low formed before the most recent structure pivot.
  * :func:`detect_order_block`      -- the last opposing candle before
    a BOS. (This is the "mitigation block" the OB-retest strategy
    returns to.)
  * :func:`above_moving_average`    -- multi-timeframe trend filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from eta_engine.strategies.models import Bar, Side


class SweepSide(StrEnum):
    """Which side of the liquidity pool was raided."""

    HIGH = "HIGH"  # wick above equal highs -> bearish sweep
    LOW = "LOW"  # wick below equal lows -> bullish sweep


# ---------------------------------------------------------------------------
# Equal highs / lows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EqualLevel:
    """Cluster of equal highs or equal lows. ``indices`` is oldest first."""

    level: float
    indices: tuple[int, ...]
    side: SweepSide

    @property
    def count(self) -> int:
        return len(self.indices)


def find_equal_levels(
    bars: list[Bar],
    *,
    tolerance_pct: float = 0.0005,
    lookback: int = 40,
    min_count: int = 2,
) -> list[EqualLevel]:
    """Find equal-high and equal-low clusters within the last ``lookback``.

    Two prices count as equal if ``abs(a - b) / a <= tolerance_pct``.
    Returns a list (possibly empty) sorted by ``count`` descending
    then most-recent-first. ``min_count`` filters out singletons.
    """
    if len(bars) < min_count:
        return []
    window = bars[-lookback:]
    offset = len(bars) - len(window)

    clusters: list[EqualLevel] = []
    for side, picker in (
        (SweepSide.HIGH, lambda b: b.high),
        (SweepSide.LOW, lambda b: b.low),
    ):
        used: set[int] = set()
        for i, bar in enumerate(window):
            if i in used:
                continue
            price = picker(bar)
            idxs = [i]
            for j in range(i + 1, len(window)):
                if j in used:
                    continue
                other = picker(window[j])
                if other == 0.0:
                    continue
                if abs(price - other) / max(abs(price), 1e-9) <= tolerance_pct:
                    idxs.append(j)
            if len(idxs) >= min_count:
                clusters.append(
                    EqualLevel(
                        level=price,
                        indices=tuple(offset + k for k in idxs),
                        side=side,
                    ),
                )
                used.update(idxs)
    clusters.sort(key=lambda c: (-c.count, -c.indices[-1]))
    return clusters


# ---------------------------------------------------------------------------
# Liquidity sweep
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiquiditySweep:
    """A wick raided an equal-level pool and price closed back inside."""

    side: SweepSide
    level: float
    sweep_bar_index: int
    close_back: float  # closing price of the sweep bar
    depth_pct: float  # how far the wick punched past ``level``


def detect_liquidity_sweep(
    bars: list[Bar],
    *,
    tolerance_pct: float = 0.0005,
    min_depth_pct: float = 0.0002,
    lookback: int = 40,
) -> LiquiditySweep | None:
    """Detect a sweep on the MOST RECENT bar.

    A bullish sweep = last bar's ``low`` punched through an equal-low
    level by >= ``min_depth_pct`` and then closed back above the level.
    """
    if len(bars) < 3:
        return None
    levels = find_equal_levels(
        bars[:-1],
        tolerance_pct=tolerance_pct,
        lookback=lookback,
    )
    last = bars[-1]
    for level in levels:
        lvl = level.level
        if level.side is SweepSide.LOW:
            depth = (lvl - last.low) / max(lvl, 1e-9)
            if last.low < lvl and last.close > lvl and depth >= min_depth_pct:
                return LiquiditySweep(
                    side=SweepSide.LOW,
                    level=lvl,
                    sweep_bar_index=len(bars) - 1,
                    close_back=last.close,
                    depth_pct=depth,
                )
        else:  # HIGH
            depth = (last.high - lvl) / max(lvl, 1e-9)
            if last.high > lvl and last.close < lvl and depth >= min_depth_pct:
                return LiquiditySweep(
                    side=SweepSide.HIGH,
                    level=lvl,
                    sweep_bar_index=len(bars) - 1,
                    close_back=last.close,
                    depth_pct=depth,
                )
    return None


# ---------------------------------------------------------------------------
# Displacement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Displacement:
    """A body-heavy candle that meaningfully exceeds local median body."""

    bar_index: int
    direction: Side
    body: float
    median_body: float
    body_mult: float


def detect_displacement(
    bars: list[Bar],
    *,
    lookback: int = 20,
    body_mult: float = 1.8,
) -> Displacement | None:
    """Check if the most recent bar is a displacement candle.

    Uses the median body of the ``lookback`` preceding bars so a single
    outlier in the history doesn't starve the signal.
    """
    if len(bars) < lookback + 1:
        return None
    window = bars[-(lookback + 1) : -1]
    bodies = sorted(bar.body for bar in window)
    median = bodies[len(bodies) // 2] if bodies else 0.0
    if median <= 0.0:
        return None
    last = bars[-1]
    mult = last.body / median
    if mult < body_mult:
        return None
    direction = Side.LONG if last.is_bull else Side.SHORT
    return Displacement(
        bar_index=len(bars) - 1,
        direction=direction,
        body=last.body,
        median_body=median,
        body_mult=mult,
    )


# ---------------------------------------------------------------------------
# Fair-value gap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FairValueGap:
    """Three-bar FVG between ``bar[-3]`` and ``bar[-1]``.

    For a bullish FVG, ``low`` is bar[-3].high, ``high`` is bar[-1].low
    and vice versa. ``filled`` is True if any subsequent bar closed back
    into the zone.
    """

    direction: Side
    low: float
    high: float
    middle_bar_index: int
    filled: bool


def detect_fvg(bars: list[Bar]) -> FairValueGap | None:
    """Return the most recent unfilled FVG, or None."""
    if len(bars) < 3:
        return None
    for i in range(len(bars) - 3, -1, -1):
        a, _, c = bars[i], bars[i + 1], bars[i + 2]
        # Bullish FVG: a.high < c.low
        if a.high < c.low:
            filled = any(bar.low <= a.high for bar in bars[i + 3 :])
            if not filled:
                return FairValueGap(
                    direction=Side.LONG,
                    low=a.high,
                    high=c.low,
                    middle_bar_index=i + 1,
                    filled=False,
                )
        # Bearish FVG: a.low > c.high
        if a.low > c.high:
            filled = any(bar.high >= a.low for bar in bars[i + 3 :])
            if not filled:
                return FairValueGap(
                    direction=Side.SHORT,
                    low=c.high,
                    high=a.low,
                    middle_bar_index=i + 1,
                    filled=False,
                )
    return None


# ---------------------------------------------------------------------------
# Break of structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BreakOfStructure:
    """Most recent BOS in either direction."""

    direction: Side
    pivot_price: float
    pivot_bar_index: int
    break_bar_index: int


def _swing_high_indices(bars: list[Bar], window: int) -> list[int]:
    return [
        i
        for i in range(window, len(bars) - window)
        if bars[i].high == max(b.high for b in bars[i - window : i + window + 1])
    ]


def _swing_low_indices(bars: list[Bar], window: int) -> list[int]:
    return [
        i
        for i in range(window, len(bars) - window)
        if bars[i].low == min(b.low for b in bars[i - window : i + window + 1])
    ]


def detect_break_of_structure(
    bars: list[Bar],
    *,
    window: int = 3,
) -> BreakOfStructure | None:
    """Return the MOST RECENT BOS.

    A bullish BOS happens when the current close takes out the most
    recent swing high. (Swing high = local max over a +/- ``window`` bar
    band.) Bearish = close below the most recent swing low.
    """
    if len(bars) < 2 * window + 2:
        return None
    last = bars[-1]
    swing_highs = _swing_high_indices(bars[:-1], window)
    if swing_highs:
        pivot_i = swing_highs[-1]
        pivot_price = bars[pivot_i].high
        if last.close > pivot_price:
            return BreakOfStructure(
                direction=Side.LONG,
                pivot_price=pivot_price,
                pivot_bar_index=pivot_i,
                break_bar_index=len(bars) - 1,
            )
    swing_lows = _swing_low_indices(bars[:-1], window)
    if swing_lows:
        pivot_i = swing_lows[-1]
        pivot_price = bars[pivot_i].low
        if last.close < pivot_price:
            return BreakOfStructure(
                direction=Side.SHORT,
                pivot_price=pivot_price,
                pivot_bar_index=pivot_i,
                break_bar_index=len(bars) - 1,
            )
    return None


# ---------------------------------------------------------------------------
# Order block
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderBlock:
    """Last opposing-direction candle before a BOS.

    For a bullish BOS the order block is the last DOWN candle immediately
    preceding the impulse leg. Entries are typically filled on retest
    of [``low``, ``high``].
    """

    direction: Side  # direction of the subsequent impulse
    bar_index: int
    low: float
    high: float


def detect_order_block(
    bars: list[Bar],
    bos: BreakOfStructure,
) -> OrderBlock | None:
    """Locate the mitigation block for a given BOS.

    Walks back from ``bos.pivot_bar_index`` looking for the last candle
    that closed *against* the impulse direction.
    """
    if bos.direction is Side.LONG:
        for i in range(bos.pivot_bar_index, -1, -1):
            bar = bars[i]
            if bar.is_bear:
                return OrderBlock(
                    direction=Side.LONG,
                    bar_index=i,
                    low=bar.low,
                    high=bar.high,
                )
    if bos.direction is Side.SHORT:
        for i in range(bos.pivot_bar_index, -1, -1):
            bar = bars[i]
            if bar.is_bull:
                return OrderBlock(
                    direction=Side.SHORT,
                    bar_index=i,
                    low=bar.low,
                    high=bar.high,
                )
    return None


# ---------------------------------------------------------------------------
# Moving average
# ---------------------------------------------------------------------------


def simple_ma(bars: list[Bar], period: int) -> float:
    """Simple moving average of closes over the last ``period`` bars.

    Returns 0.0 if the window is short.
    """
    if period <= 0 or len(bars) < period:
        return 0.0
    return sum(bar.close for bar in bars[-period:]) / float(period)


def above_moving_average(
    bars: list[Bar],
    period: int = 200,
) -> Side:
    """Return :class:`Side.LONG` if last close > MA, SHORT if below,
    :class:`Side.FLAT` if there's not enough data or price pins the MA."""
    ma = simple_ma(bars, period)
    if ma <= 0.0:
        return Side.FLAT
    last = bars[-1].close
    if last > ma:
        return Side.LONG
    if last < ma:
        return Side.SHORT
    return Side.FLAT
