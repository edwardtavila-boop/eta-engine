"""Macro / economic calendar awareness (Tier-1 #2, 2026-04-27).

Hardcoded tier-A event calendar so JARVIS can refuse / tighten size
during the windows where realized vol is materially elevated and
news-driven gaps are likely:

  * FOMC rate decisions + Powell pressers
  * NFP / Unemployment (first Friday)
  * CPI / PPI releases
  * Treasury auctions (10Y, 30Y when stressed)
  * Major earnings (FAANG-equivalent, for index-correlation)

Operator updates the dates list ahead of each year. The event window
is configurable; default is +/- 30 minutes around release time, with
a doubled window for FOMC press conferences (high-uncertainty tails).

Usage from a candidate policy or pre-flight::

    from eta_engine.brain.jarvis_v3.macro_calendar import (
        is_within_event_window, MacroEvent,
    )

    blackout = is_within_event_window(when=datetime.now(UTC), window_min=30)
    if blackout:
        return _build(Verdict.DEFERRED, ...)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class MacroEventKind(StrEnum):
    FOMC = "FOMC"
    FOMC_PRESSER = "FOMC_PRESSER"
    NFP = "NFP"
    CPI = "CPI"
    PPI = "PPI"
    GDP = "GDP"
    UNEMPLOYMENT = "UNEMPLOYMENT"
    JOLTS = "JOLTS"
    EARNINGS_TIER_A = "EARNINGS_TIER_A"  # FAANG/MSFT/NVDA/AVGO
    EARNINGS_TIER_B = "EARNINGS_TIER_B"  # broader S&P leaders
    TREASURY_AUCTION = "TREASURY_AUCTION"


@dataclass(frozen=True)
class MacroEvent:
    """One scheduled macro event. ``ts`` is in UTC."""

    kind: MacroEventKind
    ts: datetime
    name: str
    risk_level: str  # "low" | "med" | "high"
    notes: str = ""


# 2026 USA macro calendar (UTC). Update annually -- the calendar should
# be regenerated each Dec for the year ahead.
#
# FOMC: 8 meetings/year on Tue-Wed; statement at 14:00 ET (18:00 UTC),
# presser at 14:30 ET (18:30 UTC) on the Wed.
# NFP: 8:30 ET first Friday of each month -> 12:30 UTC
# CPI: 8:30 ET ~mid-month -> 12:30 UTC
_2026_EVENTS: list[MacroEvent] = [
    # ---- FOMC 2026 (illustrative dates; operator confirms each year) ----
    MacroEvent(MacroEventKind.FOMC, datetime(2026, 1, 28, 19, 0, tzinfo=UTC), "FOMC Jan", "high"),
    MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 1, 28, 19, 30, tzinfo=UTC), "Powell Jan", "high"),
    MacroEvent(MacroEventKind.FOMC, datetime(2026, 3, 18, 18, 0, tzinfo=UTC), "FOMC Mar", "high"),
    MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 3, 18, 18, 30, tzinfo=UTC), "Powell Mar", "high"),
    MacroEvent(MacroEventKind.FOMC, datetime(2026, 4, 29, 18, 0, tzinfo=UTC), "FOMC Apr", "high"),
    MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 4, 29, 18, 30, tzinfo=UTC), "Powell Apr", "high"),
    MacroEvent(MacroEventKind.FOMC, datetime(2026, 6, 17, 18, 0, tzinfo=UTC), "FOMC Jun", "high"),
    MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 6, 17, 18, 30, tzinfo=UTC), "Powell Jun", "high"),
    MacroEvent(MacroEventKind.FOMC, datetime(2026, 7, 29, 18, 0, tzinfo=UTC), "FOMC Jul", "high"),
    MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 7, 29, 18, 30, tzinfo=UTC), "Powell Jul", "high"),
    MacroEvent(MacroEventKind.FOMC, datetime(2026, 9, 16, 18, 0, tzinfo=UTC), "FOMC Sep", "high"),
    MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 9, 16, 18, 30, tzinfo=UTC), "Powell Sep", "high"),
    MacroEvent(MacroEventKind.FOMC, datetime(2026, 11, 4, 19, 0, tzinfo=UTC), "FOMC Nov", "high"),
    MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 11, 4, 19, 30, tzinfo=UTC), "Powell Nov", "high"),
    MacroEvent(MacroEventKind.FOMC, datetime(2026, 12, 16, 19, 0, tzinfo=UTC), "FOMC Dec", "high"),
    MacroEvent(MacroEventKind.FOMC_PRESSER, datetime(2026, 12, 16, 19, 30, tzinfo=UTC), "Powell Dec", "high"),
    # ---- NFP 2026 (first Friday of each month, 8:30 ET = 12:30 UTC) ----
    MacroEvent(MacroEventKind.NFP, datetime(2026, 1, 2, 13, 30, tzinfo=UTC), "NFP Jan", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 2, 6, 13, 30, tzinfo=UTC), "NFP Feb", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 3, 6, 13, 30, tzinfo=UTC), "NFP Mar", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 4, 3, 12, 30, tzinfo=UTC), "NFP Apr", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 5, 1, 12, 30, tzinfo=UTC), "NFP May", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 6, 5, 12, 30, tzinfo=UTC), "NFP Jun", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 7, 3, 12, 30, tzinfo=UTC), "NFP Jul", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 8, 7, 12, 30, tzinfo=UTC), "NFP Aug", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 9, 4, 12, 30, tzinfo=UTC), "NFP Sep", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 10, 2, 12, 30, tzinfo=UTC), "NFP Oct", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 11, 6, 13, 30, tzinfo=UTC), "NFP Nov", "high"),
    MacroEvent(MacroEventKind.NFP, datetime(2026, 12, 4, 13, 30, tzinfo=UTC), "NFP Dec", "high"),
    # ---- CPI 2026 (~mid-month, 8:30 ET = 12:30 UTC) ----
    MacroEvent(MacroEventKind.CPI, datetime(2026, 1, 14, 13, 30, tzinfo=UTC), "CPI Jan", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 2, 12, 13, 30, tzinfo=UTC), "CPI Feb", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 3, 12, 12, 30, tzinfo=UTC), "CPI Mar", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 4, 14, 12, 30, tzinfo=UTC), "CPI Apr", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 5, 13, 12, 30, tzinfo=UTC), "CPI May", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 6, 11, 12, 30, tzinfo=UTC), "CPI Jun", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 7, 15, 12, 30, tzinfo=UTC), "CPI Jul", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 8, 13, 12, 30, tzinfo=UTC), "CPI Aug", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 9, 10, 12, 30, tzinfo=UTC), "CPI Sep", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 10, 15, 12, 30, tzinfo=UTC), "CPI Oct", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 11, 12, 13, 30, tzinfo=UTC), "CPI Nov", "high"),
    MacroEvent(MacroEventKind.CPI, datetime(2026, 12, 10, 13, 30, tzinfo=UTC), "CPI Dec", "high"),
]


# Per-event default window in MINUTES on each side of ``ts``.
_DEFAULT_WINDOW_MIN: dict[MacroEventKind, int] = {
    MacroEventKind.FOMC: 30,
    MacroEventKind.FOMC_PRESSER: 60,  # presser is the high-uncertainty tail
    MacroEventKind.NFP: 30,
    MacroEventKind.CPI: 30,
    MacroEventKind.PPI: 15,
    MacroEventKind.GDP: 15,
    MacroEventKind.UNEMPLOYMENT: 15,
    MacroEventKind.JOLTS: 10,
    MacroEventKind.EARNINGS_TIER_A: 30,
    MacroEventKind.EARNINGS_TIER_B: 15,
    MacroEventKind.TREASURY_AUCTION: 15,
}


def is_within_event_window(
    when: datetime,
    *,
    events: list[MacroEvent] | None = None,
    window_min_override: int | None = None,
) -> MacroEvent | None:
    """Return the matching event if ``when`` falls within any event's
    +/- window, else None.

    Default windows are per-kind via ``_DEFAULT_WINDOW_MIN``;
    ``window_min_override`` overrides if supplied.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    pool = events if events is not None else _2026_EVENTS
    for e in pool:
        win = window_min_override if window_min_override is not None else _DEFAULT_WINDOW_MIN.get(e.kind, 30)
        delta = abs((when - e.ts).total_seconds())
        if delta <= win * 60:
            return e
    return None


def upcoming_events(*, hours_ahead: float = 24.0) -> list[MacroEvent]:
    """Return all events scheduled within ``hours_ahead`` hours of now."""
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=hours_ahead)
    return [e for e in _2026_EVENTS if now <= e.ts <= horizon]
