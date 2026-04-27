"""EVOLUTIONARY TRADING ALGO  //  core.events_calendar.

News/events calendar enrichment for the session filter + confluence scorer.

Why this module exists
----------------------
:mod:`core.session_filter` has ``NewsEvent`` + ``HIGH_IMPACT_TAGS`` but no
scheduled-events feed. The events come from a flat config today, and the
trading session has no awareness of which events are imminent, which are
behind us, or what the pre-event quiet-window policy should be.

This module adds:

* **A typed event calendar.** :class:`EventsCalendar` owns an ordered
  list of :class:`CalendarEvent` records, with an O(log n) `next_event`
  lookup and window-aware filters.
* **MCP-first loader.** :func:`load_from_mcp` consumes a
  `bigdata`-shaped events payload (the ``bigdata_events_calendar`` MCP
  tool) and maps into our schema. Falls back to a local JSON schedule
  when the MCP tap is unavailable.
* **Blackout helpers.** ``blackout_active(now)`` tells the session
  filter whether any high-impact event is inside its pre-/post-window;
  ``minutes_to_next(now)`` gives the countdown for the scorer.

Keeping this in its own module lets the session filter stay deterministic
while calendar data is refreshed on a slower cadence (5m).
"""

from __future__ import annotations

import bisect
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger(__name__)


__all__ = [
    "CalendarEvent",
    "EventsCalendar",
    "BigDataMcp",
    "DEFAULT_PRE_WINDOW_MIN",
    "DEFAULT_POST_WINDOW_MIN",
    "HIGH_IMPACT_TAGS",
    "load_from_json",
    "load_from_mcp",
]


DEFAULT_PRE_WINDOW_MIN: int = 15
DEFAULT_POST_WINDOW_MIN: int = 15

HIGH_IMPACT_TAGS: tuple[str, ...] = (
    "FOMC",
    "CPI",
    "PCE",
    "NFP",
    "GDP",
    "PPI",
    "ISM",
    "FED_SPEAK",
    "EARNINGS_MEGA_CAP",
    "OPEX",
    "TSY_AUCTION",
)


@dataclass(frozen=True)
class CalendarEvent:
    """One scheduled high-impact macro or earnings event."""

    tag: str  # e.g. "FOMC", "CPI"
    scheduled_utc: datetime
    impact: str = "high"  # high | medium | low
    description: str = ""
    source: str = "manual"  # bigdata | manual | firm_override

    def in_window(
        self,
        now: datetime,
        *,
        pre_minutes: int = DEFAULT_PRE_WINDOW_MIN,
        post_minutes: int = DEFAULT_POST_WINDOW_MIN,
    ) -> bool:
        """True when `now` sits inside the pre/post blackout window."""
        start = self.scheduled_utc - timedelta(minutes=max(0, pre_minutes))
        end = self.scheduled_utc + timedelta(minutes=max(0, post_minutes))
        return start <= now <= end


class BigDataMcp(Protocol):
    """Subset of the bigdata MCP needed to build the calendar."""

    def events_calendar(
        self,
        *,
        start_utc: datetime,
        end_utc: datetime,
        impact: str = "high",
    ) -> list[dict[str, Any]]: ...


@dataclass
class EventsCalendar:
    """Ordered high-impact event schedule. Mutable; filters are pure."""

    events: list[CalendarEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.events = sorted(self.events, key=lambda e: e.scheduled_utc)

    def add(self, event: CalendarEvent) -> None:
        self.events.append(event)
        self.events.sort(key=lambda e: e.scheduled_utc)

    def extend(self, events: Iterable[CalendarEvent]) -> None:
        for event in events:
            self.events.append(event)
        self.events.sort(key=lambda e: e.scheduled_utc)

    def next_event(
        self,
        now: datetime,
        *,
        impact_filter: str | None = "high",
    ) -> CalendarEvent | None:
        """Return the next event at or after `now`. O(log n)."""
        if not self.events:
            return None
        times = [e.scheduled_utc for e in self.events]
        idx = bisect.bisect_left(times, now)
        while idx < len(self.events):
            cand = self.events[idx]
            if impact_filter is None or cand.impact == impact_filter:
                return cand
            idx += 1
        return None

    def minutes_to_next(
        self,
        now: datetime,
        *,
        impact_filter: str | None = "high",
    ) -> float | None:
        """Minutes until the next qualifying event. ``None`` if no event remains."""
        nxt = self.next_event(now, impact_filter=impact_filter)
        if nxt is None:
            return None
        delta = nxt.scheduled_utc - now
        return delta.total_seconds() / 60.0

    def blackout_active(
        self,
        now: datetime,
        *,
        pre_minutes: int = DEFAULT_PRE_WINDOW_MIN,
        post_minutes: int = DEFAULT_POST_WINDOW_MIN,
        impact_filter: str | None = "high",
    ) -> bool:
        """True if any qualifying event's pre/post window contains `now`."""
        # Only need to check events within [-post, +pre] of now.
        lo = now - timedelta(minutes=max(0, post_minutes))
        hi = now + timedelta(minutes=max(0, pre_minutes))
        times = [e.scheduled_utc for e in self.events]
        start_idx = bisect.bisect_left(times, lo)
        end_idx = bisect.bisect_right(times, hi)
        for event in self.events[start_idx:end_idx]:
            if impact_filter is not None and event.impact != impact_filter:
                continue
            if event.in_window(now, pre_minutes=pre_minutes, post_minutes=post_minutes):
                return True
        return False

    def active_event(
        self,
        now: datetime,
        *,
        pre_minutes: int = DEFAULT_PRE_WINDOW_MIN,
        post_minutes: int = DEFAULT_POST_WINDOW_MIN,
        impact_filter: str | None = "high",
    ) -> CalendarEvent | None:
        """Return the single event whose window contains `now`, if any."""
        lo = now - timedelta(minutes=max(0, post_minutes))
        hi = now + timedelta(minutes=max(0, pre_minutes))
        times = [e.scheduled_utc for e in self.events]
        start_idx = bisect.bisect_left(times, lo)
        end_idx = bisect.bisect_right(times, hi)
        for event in self.events[start_idx:end_idx]:
            if impact_filter is not None and event.impact != impact_filter:
                continue
            if event.in_window(now, pre_minutes=pre_minutes, post_minutes=post_minutes):
                return event
        return None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_from_json(path: str | Path) -> EventsCalendar:
    """Load an events calendar from a local JSON schedule.

    Expected schema::

        [
            {"tag": "FOMC", "scheduled_utc": "2026-05-01T18:00:00Z",
             "impact": "high", "description": "FOMC rate decision"},
            ...
        ]
    """
    p = Path(path)
    if not p.exists():
        return EventsCalendar()
    raw = json.loads(p.read_text())
    events: list[CalendarEvent] = []
    for item in raw:
        try:
            ts = _parse_ts(item.get("scheduled_utc") or item.get("time"))
        except ValueError:
            log.warning("calendar skip: unparseable ts in %r", item)
            continue
        events.append(
            CalendarEvent(
                tag=str(item.get("tag") or item.get("name") or "UNKNOWN").upper(),
                scheduled_utc=ts,
                impact=str(item.get("impact") or "high").lower(),
                description=str(item.get("description") or item.get("desc") or ""),
                source=str(item.get("source") or "manual"),
            )
        )
    return EventsCalendar(events=events)


def load_from_mcp(
    mcp: BigDataMcp,
    *,
    start_utc: datetime | None = None,
    days_ahead: int = 14,
    impact_filter: str = "high",
) -> EventsCalendar:
    """Pull the next `days_ahead` days of events from the bigdata MCP."""
    start = start_utc or datetime.now(UTC)
    end = start + timedelta(days=max(1, days_ahead))
    try:
        rows = mcp.events_calendar(start_utc=start, end_utc=end, impact=impact_filter)
    except Exception as exc:  # noqa: BLE001
        log.warning("events_calendar MCP failed: %s", exc)
        return EventsCalendar()
    events: list[CalendarEvent] = []
    for row in rows or []:
        try:
            ts = _parse_ts(row.get("scheduled_utc") or row.get("ts") or row.get("event_time"))
        except ValueError:
            continue
        events.append(
            CalendarEvent(
                tag=str(row.get("tag") or row.get("name") or "UNKNOWN").upper(),
                scheduled_utc=ts,
                impact=str(row.get("impact") or impact_filter).lower(),
                description=str(row.get("description") or ""),
                source="bigdata",
            )
        )
    return EventsCalendar(events=events)


def _parse_ts(raw: datetime | str) -> datetime:
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw.astimezone(UTC)
    if isinstance(raw, str):
        s = raw.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(UTC)
    raise ValueError(f"unparseable timestamp: {raw!r}")
