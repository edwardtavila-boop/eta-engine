"""Tests for core.events_calendar."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from eta_engine.core.events_calendar import (
    DEFAULT_POST_WINDOW_MIN,
    DEFAULT_PRE_WINDOW_MIN,
    CalendarEvent,
    EventsCalendar,
    load_from_json,
    load_from_mcp,
)

if TYPE_CHECKING:
    from pathlib import Path


def _now() -> datetime:
    return datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _event(*, tag: str = "FOMC", minutes_offset: int = 0, impact: str = "high") -> CalendarEvent:
    return CalendarEvent(
        tag=tag,
        scheduled_utc=_now() + timedelta(minutes=minutes_offset),
        impact=impact,
    )


class TestCalendarEventInWindow:
    def test_inside_pre_window(self):
        ev = _event(minutes_offset=10)  # 10 min ahead
        assert ev.in_window(_now(), pre_minutes=15, post_minutes=15) is True

    def test_inside_post_window(self):
        ev = _event(minutes_offset=-10)  # 10 min behind
        assert ev.in_window(_now(), pre_minutes=15, post_minutes=15) is True

    def test_outside_windows(self):
        ev = _event(minutes_offset=60)  # 1h ahead
        assert ev.in_window(_now(), pre_minutes=15, post_minutes=15) is False


class TestEventsCalendar:
    def test_next_event_returns_soonest(self):
        c = EventsCalendar(
            events=[
                _event(tag="CPI", minutes_offset=30),
                _event(tag="FOMC", minutes_offset=120),
            ]
        )
        nxt = c.next_event(_now())
        assert nxt is not None
        assert nxt.tag == "CPI"

    def test_next_event_skips_past(self):
        c = EventsCalendar(
            events=[
                _event(tag="PAST", minutes_offset=-30),
                _event(tag="FUTURE", minutes_offset=30),
            ]
        )
        nxt = c.next_event(_now())
        assert nxt is not None
        assert nxt.tag == "FUTURE"

    def test_next_event_none_when_all_past(self):
        c = EventsCalendar(events=[_event(minutes_offset=-60)])
        assert c.next_event(_now()) is None

    def test_minutes_to_next_future(self):
        c = EventsCalendar(events=[_event(minutes_offset=45)])
        minutes = c.minutes_to_next(_now())
        assert minutes == pytest.approx(45.0)

    def test_blackout_active_inside_pre_window(self):
        c = EventsCalendar(events=[_event(minutes_offset=10)])
        assert c.blackout_active(_now(), pre_minutes=15, post_minutes=15) is True

    def test_blackout_inactive_outside_windows(self):
        c = EventsCalendar(events=[_event(minutes_offset=60)])
        assert c.blackout_active(_now(), pre_minutes=15, post_minutes=15) is False

    def test_blackout_respects_impact_filter(self):
        c = EventsCalendar(events=[_event(minutes_offset=5, impact="low")])
        # high-only filter, so a low-impact event in-window still isn't a blackout
        assert c.blackout_active(_now(), impact_filter="high") is False
        assert c.blackout_active(_now(), impact_filter=None) is True

    def test_active_event_returns_single_event(self):
        c = EventsCalendar(
            events=[
                _event(tag="NFP", minutes_offset=-5),  # in post-window
                _event(tag="CPI", minutes_offset=120),  # far future
            ]
        )
        active = c.active_event(_now())
        assert active is not None
        assert active.tag == "NFP"

    def test_add_preserves_sort_order(self):
        c = EventsCalendar()
        c.add(_event(tag="LATE", minutes_offset=60))
        c.add(_event(tag="EARLY", minutes_offset=10))
        tags = [e.tag for e in c.events]
        assert tags == ["EARLY", "LATE"]

    def test_empty_calendar_safe(self):
        c = EventsCalendar()
        assert c.next_event(_now()) is None
        assert c.minutes_to_next(_now()) is None
        assert c.blackout_active(_now()) is False


class TestLoadFromJson:
    def test_loads_valid_schedule(self, tmp_path: Path):
        path = tmp_path / "events.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "tag": "FOMC",
                        "scheduled_utc": "2026-05-01T18:00:00Z",
                        "impact": "high",
                    },
                    {
                        "tag": "CPI",
                        "scheduled_utc": "2026-05-02T12:30:00Z",
                        "impact": "high",
                    },
                ]
            )
        )
        c = load_from_json(path)
        assert len(c.events) == 2
        assert c.events[0].tag == "FOMC"
        assert c.events[1].tag == "CPI"

    def test_nonexistent_path_returns_empty_calendar(self, tmp_path: Path):
        c = load_from_json(tmp_path / "missing.json")
        assert c.events == []

    def test_skips_unparseable_entries(self, tmp_path: Path):
        path = tmp_path / "events.json"
        path.write_text(
            json.dumps(
                [
                    {"tag": "OK", "scheduled_utc": "2026-05-01T18:00:00Z"},
                    {"tag": "BROKEN", "scheduled_utc": "not-a-time"},
                ]
            )
        )
        c = load_from_json(path)
        assert len(c.events) == 1
        assert c.events[0].tag == "OK"


class FakeBigDataMcp:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def events_calendar(self, *, start_utc: datetime, end_utc: datetime, impact: str = "high") -> list[dict[str, Any]]:
        return self._rows


class TestLoadFromMcp:
    def test_loads_rows(self):
        mcp = FakeBigDataMcp(
            rows=[
                {
                    "tag": "FOMC",
                    "scheduled_utc": "2026-05-01T18:00:00Z",
                    "impact": "high",
                    "description": "FOMC rate decision",
                }
            ]
        )
        c = load_from_mcp(mcp)
        assert len(c.events) == 1
        assert c.events[0].source == "bigdata"

    def test_handles_mcp_failure_gracefully(self):
        class Broken:
            def events_calendar(self, **kwargs):
                raise RuntimeError("MCP down")

        c = load_from_mcp(Broken())
        assert c.events == []


class TestWindowDefaults:
    def test_defaults_are_fifteen_minutes(self):
        assert DEFAULT_PRE_WINDOW_MIN == 15
        assert DEFAULT_POST_WINDOW_MIN == 15
