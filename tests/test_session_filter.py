"""
EVOLUTIONARY TRADING ALGO  //  tests.test_session_filter
============================================
Session windows and news blackout gates.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from eta_engine.core.session_filter import (
    HIGH_IMPACT_TAGS,
    NewsEvent,
    SessionWindow,
    is_htf_window,
    is_news_blackout,
)

# ---------------------------------------------------------------------------
# Session windows
# ---------------------------------------------------------------------------


class TestSessionWindow:
    def test_session_window_has_required_fields(self) -> None:
        w = SessionWindow(name="ny_open", start_time=time(9, 30), end_time=time(11, 30))
        assert w.name == "ny_open"
        assert w.start_time == time(9, 30)
        assert w.end_time == time(11, 30)

    def test_ny_open_is_htf(self) -> None:
        """13:30 UTC = 9:30 AM ET -> inside ny_open_htf window."""
        dt = datetime(2025, 3, 10, 14, 0, tzinfo=UTC)
        result = is_htf_window(dt)
        assert result is not None
        assert result.name == "ny_open_htf"

    def test_overnight_not_htf(self) -> None:
        """2:00 AM UTC -- no major session active."""
        dt = datetime(2025, 3, 10, 2, 0, tzinfo=UTC)
        result = is_htf_window(dt)
        assert result is None

    def test_london_open_detected(self) -> None:
        """7:30 UTC -> inside london_open_htf."""
        dt = datetime(2025, 3, 10, 7, 30, tzinfo=UTC)
        result = is_htf_window(dt)
        assert result is not None
        assert result.name == "london_open_htf"


# ---------------------------------------------------------------------------
# News blackout
# ---------------------------------------------------------------------------


class TestNewsBlackout:
    def test_fomc_is_high_impact(self) -> None:
        assert "FOMC" in HIGH_IMPACT_TAGS

    def test_blackout_with_no_events(self) -> None:
        result = is_news_blackout(datetime.now(UTC), events=[])
        assert result is False

    def test_blackout_during_event(self) -> None:
        """Inside the blackout window of a high-impact event."""
        event_time = datetime(2025, 3, 10, 14, 0, tzinfo=UTC)
        event = NewsEvent(tag="FOMC", scheduled_time=event_time)
        check_time = datetime(2025, 3, 10, 14, 5, tzinfo=UTC)
        result = is_news_blackout(check_time, events=[event])
        assert result is True

    def test_no_blackout_far_from_event(self) -> None:
        """Well outside the blackout window."""
        event_time = datetime(2025, 3, 10, 14, 0, tzinfo=UTC)
        event = NewsEvent(tag="NFP", scheduled_time=event_time)
        check_time = datetime(2025, 3, 10, 10, 0, tzinfo=UTC)
        result = is_news_blackout(check_time, events=[event])
        assert result is False

    def test_non_high_impact_ignored(self) -> None:
        """Events not in HIGH_IMPACT_TAGS should not trigger blackout."""
        event_time = datetime(2025, 3, 10, 14, 0, tzinfo=UTC)
        event = NewsEvent(tag="random_report", scheduled_time=event_time)
        check_time = datetime(2025, 3, 10, 14, 5, tzinfo=UTC)
        result = is_news_blackout(check_time, events=[event])
        assert result is False

    def test_high_impact_tags_not_empty(self) -> None:
        assert len(HIGH_IMPACT_TAGS) >= 3
