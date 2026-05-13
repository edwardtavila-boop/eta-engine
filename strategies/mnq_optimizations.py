"""
EVOLUTIONARY TRADING ALGO  //  strategies.mnq_optimizations
============================================================
MNQ-specific entry filters built on top of the regime gate.

Why this exists
---------------
The basic regime gate (block trending_up / trending_down) cut
losers in trending regimes but didn't push the strategy past the
strict promotion gate. Per the 2026-04-27 Window 0 deep-dive,
losers concentrate in: trending bars, opening/closing volatility,
and decoupled-from-ES sessions. This module exposes three
composable predicates so the MNQ ctx_builder can stack them:

  * ``classify_regime_v2(bars)`` — adds volatility regime
    on top of the directional regime tag. Returns one of
    ``low_vol_choppy``, ``low_vol_trend_up``, ``low_vol_trend_down``,
    ``high_vol_choppy``, ``high_vol_trend_up``, ``high_vol_trend_down``,
    ``panic`` (ATR > 3× rolling baseline), ``warmup``.
  * ``in_session(ts, profile)`` — true outside the hot zones
    (default profile: avoid first 30m + last 30m of RTH where
    open / close noise dominates). Profile is a frozen tuple of
    (start, end) blackout windows in local time.
  * ``correlated_with_es(mnq_bars, es_bars)`` — true when MNQ
    return and ES return are co-aligned in the last N bars; a
    decoupled regime is one of the worst trade environments.

These are pure functions over bar lists / timestamps / context.
They're imported by ``run_walk_forward_mnq_real._ctx`` so the
runner sets ``ctx["regime"]`` to the v2 label and adds
``ctx["session_ok"]``, ``ctx["es_aligned"]`` flags. The
BacktestEngine's regime-gate then blocks any regime that isn't
explicitly approved, while a separate engine flag (added in this
patch) blocks bars when ``session_ok`` or ``es_aligned`` are False.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# Regime classifier v2
# ---------------------------------------------------------------------------


def classify_regime_v2(
    bars: list[BarData],
    *,
    short_window: int = 20,
    long_window: int = 60,
    panic_atr_ratio: float = 3.0,
    high_vol_atr_ratio: float = 1.5,
    drift_threshold: float = 0.005,
) -> str:
    """Two-axis regime tag: directional × volatility.

    Returns one of:
      ``warmup``                  — fewer than ``long_window`` bars
      ``panic``                   — recent ATR ≥ ``panic_atr_ratio`` × baseline
      ``high_vol_trend_up``       — high vol AND drift up
      ``high_vol_trend_down``     — high vol AND drift down
      ``high_vol_choppy``         — high vol AND no clear drift
      ``low_vol_trend_up``
      ``low_vol_trend_down``
      ``low_vol_choppy``

    Default thresholds calibrated to MNQ 5m: short_window=20 bars
    (~100 min), long_window=60 (~5 hrs). drift_threshold=0.5%
    matches the v1 classifier. panic at ATR triple normal — that's
    a CPI / FOMC / unscheduled-news fingerprint.
    """
    if len(bars) < long_window:
        return "warmup"

    short = bars[-short_window:]
    longer = bars[-long_window:-short_window]
    if not short or not longer:
        return "warmup"

    short_atr = sum(b.high - b.low for b in short) / len(short)
    long_atr = sum(b.high - b.low for b in longer) / len(longer)
    if long_atr <= 0.0:
        return "warmup"
    atr_ratio = short_atr / long_atr

    if atr_ratio >= panic_atr_ratio:
        return "panic"

    drift = (short[-1].close - short[0].close) / short[0].close if short[0].close > 0.0 else 0.0

    vol_prefix = "high_vol" if atr_ratio >= high_vol_atr_ratio else "low_vol"
    if drift > drift_threshold:
        return f"{vol_prefix}_trend_up"
    if drift < -drift_threshold:
        return f"{vol_prefix}_trend_down"
    return f"{vol_prefix}_choppy"


# ---------------------------------------------------------------------------
# Session profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionProfile:
    """Local-time blackout windows; bars inside a window are blocked.

    Default profile blocks the first 30 minutes and last 30 minutes
    of MNQ RTH (08:30-09:00 and 15:30-16:00 CT). These two zones
    consistently show the worst exit-quality in the journal — open
    is too noisy, close is whipsaw from EoD positioning.
    """

    timezone_name: str
    blackout_windows: tuple[tuple[time, time], ...]

    @classmethod
    def mnq_default(cls) -> SessionProfile:
        return cls(
            timezone_name="America/Chicago",
            blackout_windows=(
                (time(8, 30), time(9, 0)),
                (time(15, 30), time(16, 0)),
            ),
        )


def _zone(profile: SessionProfile) -> ZoneInfo:
    from zoneinfo import ZoneInfo

    return ZoneInfo(profile.timezone_name)


def in_session(ts: datetime, profile: SessionProfile | None = None) -> bool:
    """True when the bar timestamp is OUTSIDE every blackout window.

    Operates entirely in the profile's local timezone. Uses
    half-open ``[start, end)`` checks so e.g. an 09:00 bar exits the
    08:30-09:00 window cleanly.
    """
    profile = profile or SessionProfile.mnq_default()
    if ts.tzinfo is None:
        # assume UTC for legacy callers
        from datetime import UTC

        ts = ts.replace(tzinfo=UTC)
    local = ts.astimezone(_zone(profile)).timetz()
    local_t = time(local.hour, local.minute, local.second)
    return all(not start <= local_t < end for start, end in profile.blackout_windows)


# ---------------------------------------------------------------------------
# ES correlation gate
# ---------------------------------------------------------------------------


def correlated_with_es(
    mnq_bars: list[BarData],
    es_bars: list[BarData] | None,
    *,
    window: int = 30,
    threshold: float = 0.4,
) -> bool:
    """True when recent MNQ-ES correlation ≥ ``threshold``.

    When ``es_bars`` is None or insufficient, returns ``True`` —
    a missing correlation feed should not gate trading. This way
    the filter is "best effort" and the strategy continues to run
    at slightly worse quality if ES data isn't available.

    The default threshold (0.4) is intentionally permissive. MNQ
    and ES are usually >0.6 correlated intraday; a sub-0.4
    correlation typically means a sector-specific Nasdaq event
    (mega-cap earnings, AI selloff) where the index correlation
    breaks and our cross-asset features go stale.
    """
    if not es_bars or len(mnq_bars) < window or len(es_bars) < window:
        return True
    mnq_returns = _bar_returns(mnq_bars[-window:])
    es_returns = _bar_returns(es_bars[-window:])
    if len(mnq_returns) < 2 or len(es_returns) < 2:
        return True
    n = min(len(mnq_returns), len(es_returns))
    return _pearson(mnq_returns[-n:], es_returns[-n:]) >= threshold


def _bar_returns(bars: list[BarData]) -> list[float]:
    return [(b2.close - b1.close) / b1.close for b1, b2 in zip(bars[:-1], bars[1:], strict=False) if b1.close > 0.0]


def _pearson(a: list[float], b: list[float]) -> float:
    """Pearson correlation; returns 0.0 on any degenerate input."""
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)
    num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= 0.0 or var_b <= 0.0:
        return 0.0
    return num / ((var_a * var_b) ** 0.5)
