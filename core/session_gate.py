"""
EVOLUTIONARY TRADING ALGO  //  core.session_gate
====================================
The single source of truth for "may we open a new position right now, and
if we have one open, do we need to flatten it before the close?"

Why this module exists
----------------------
The bot's per-bar dispatch checks ``StrategyContext.session_allows_entries``.
That flag has historically been set to ``True`` everywhere because we had
no central place to compute it against:

  * the RTH window (MNQ only earns edge during US regular hours on our
    real-MNQ sweep -- see docs/cross_regime/REAL_MNQ_TRANSFER_20260424.md)
  * the news blackout (``core.events_calendar``)
  * the EoD flatten cutoff (~3:59 ET) -- Apex eval risk requires no
    overnight carry; leaving a position open through the close is a
    preventable DD event.

``SessionGate`` fuses all three into two deterministic checks:

  * ``entries_allowed(now)`` -> ``(bool, reason)``
  * ``should_flatten_eod(now)`` -> ``(bool, reason)``

The bot calls ``entries_allowed`` every bar to set
``session_allows_entries``; it calls ``should_flatten_eod`` every bar to
decide whether to emit an ``EXIT_ALL`` marker.

All times internally are UTC; the gate converts once per call. A
``zoneinfo``-backed config keeps DST transitions correct (no hand-
rolled offset math).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from eta_engine.core.events_calendar import EventsCalendar

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reason tags -- small closed set so callers can enum-match for metrics.
# ---------------------------------------------------------------------------

REASON_ALLOWED = "allowed"
REASON_OUTSIDE_RTH = "outside_rth"
REASON_NEWS_BLACKOUT = "news_blackout"
REASON_EOD_CUTOFF = "eod_cutoff"

REASON_EOD_PENDING = "eod_pending"
REASON_EOD_NOT_DUE = "no_eod_action"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionGateConfig:
    """Tunable policy for the live session gate.

    Defaults target MNQ on IBKR (the active default futures venue while
    Tradovate is DORMANT per operator mandate 2026-04-24), RTH-only
    trading, Apex-eval-aware EoD flatten. Other venues/symbols can
    instantiate with overrides.
    """

    # Timezone used to interpret all *_local times. Must be a valid IANA
    # zone so DST is handled automatically.
    timezone_name: str = "America/Chicago"

    # RTH window in the configured timezone. Entries are blocked outside
    # this window. For MNQ RTH = 08:30-15:00 CT.
    rth_start_local: time = time(8, 30)
    rth_end_local: time = time(15, 0)

    # EoD cutoff. At or after this local time, the gate:
    #   * blocks new entries (entries_allowed -> False)
    #   * signals flatten (should_flatten_eod -> True)
    # Default 15:59 CT = 1 minute before the RTH close. A 1-minute
    # buffer is enough for one round-turn on MNQ.
    eod_cutoff_local: time = time(15, 59)

    # News blackout windows. Applied to any HIGH-impact event in the
    # attached calendar.
    news_pre_minutes: int = 15
    news_post_minutes: int = 15

    # If True, entries are blocked during the blackout. Flatten is NOT
    # forced by news (distinct from EoD flatten) -- that's a policy
    # decision the operator makes explicitly.
    block_entries_during_news: bool = True


@dataclass
class SessionGate:
    """Pure-policy gate. Stateless; all checks take ``now`` explicitly."""

    config: SessionGateConfig = field(default_factory=SessionGateConfig)
    calendar: EventsCalendar | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def entries_allowed(self, now: datetime) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` where ``reason`` is a tag plus
        optional detail (e.g. ``"news_blackout:FOMC"``).
        """
        now_utc = _to_utc(now)
        local = now_utc.astimezone(self._tz())
        local_time = local.time()

        # Check RTH window first (cheapest check; no calendar needed).
        if not _within_window(
            local_time,
            self.config.rth_start_local,
            self.config.rth_end_local,
        ):
            return False, REASON_OUTSIDE_RTH

        # EoD cutoff is a tighter right edge than the RTH end. Within
        # the RTH window but past the cutoff -> block new entries.
        if local_time >= self.config.eod_cutoff_local:
            return False, REASON_EOD_CUTOFF

        # News blackout (optional; no calendar = no blackout).
        if self.config.block_entries_during_news and self.calendar is not None:
            event = self.calendar.active_event(
                now_utc,
                pre_minutes=self.config.news_pre_minutes,
                post_minutes=self.config.news_post_minutes,
                impact_filter="high",
            )
            if event is not None:
                return False, f"{REASON_NEWS_BLACKOUT}:{event.tag}"

        return True, REASON_ALLOWED

    def should_flatten_eod(self, now: datetime) -> tuple[bool, str]:
        """True when we've crossed the EoD cutoff and still hold risk.

        The bot is responsible for calling this every bar during RTH;
        when it returns ``(True, ...)`` the bot should emit EXIT_ALL
        for any open positions and refuse new entries until the next
        RTH session.
        """
        now_utc = _to_utc(now)
        local = now_utc.astimezone(self._tz())
        local_time = local.time()

        # Only meaningful within the RTH bounds. Outside of RTH, the
        # overnight/pre-market code path already blocks entries and we
        # assume no positions remain.
        if not _within_window(
            local_time,
            self.config.rth_start_local,
            self.config.rth_end_local,
        ):
            return False, REASON_EOD_NOT_DUE
        if local_time >= self.config.eod_cutoff_local:
            return True, REASON_EOD_PENDING
        return False, REASON_EOD_NOT_DUE

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.config.timezone_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _within_window(check: time, start: time, end: time) -> bool:
    """Half-open [start, end) with midnight-wrap support.

    MNQ RTH does not wrap, but this keeps the helper reusable for
    symbols whose session crosses midnight local (e.g. crypto).
    """
    if start <= end:
        return start <= check < end
    return check >= start or check < end


__all__ = [
    "REASON_ALLOWED",
    "REASON_EOD_CUTOFF",
    "REASON_EOD_NOT_DUE",
    "REASON_EOD_PENDING",
    "REASON_NEWS_BLACKOUT",
    "REASON_OUTSIDE_RTH",
    "SessionGate",
    "SessionGateConfig",
]
