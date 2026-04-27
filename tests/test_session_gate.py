"""Tests for core.session_gate -- RTH / news / EoD unified entry gate."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from eta_engine.core.events_calendar import CalendarEvent, EventsCalendar
from eta_engine.core.session_gate import (
    REASON_ALLOWED,
    REASON_EOD_CUTOFF,
    REASON_EOD_NOT_DUE,
    REASON_EOD_PENDING,
    REASON_NEWS_BLACKOUT,
    REASON_OUTSIDE_RTH,
    SessionGate,
    SessionGateConfig,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _ct_to_utc(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    """Interpret (hh:mm) as America/Chicago and return UTC.

    Using hand-rolled dates safely-placed in CDT (March-November) where
    CT = UTC-5. Keeps tests DST-independent. Uses timedelta so
    late-evening CT hours don't overflow the hour slot.
    """
    # 2026-05-15 CDT = UTC-5
    return datetime(y, m, d, hh, mm, tzinfo=UTC) + timedelta(hours=5)


# --------------------------------------------------------------------------- #
# RTH gating
# --------------------------------------------------------------------------- #
class TestRthWindow:
    def test_rth_mid_day_is_allowed(self) -> None:
        gate = SessionGate()
        # 10:00 CT is deep in RTH
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 10, 0))
        assert ok is True
        assert reason == REASON_ALLOWED

    def test_pre_rth_is_blocked(self) -> None:
        gate = SessionGate()
        # 08:00 CT is 30min before RTH
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 8, 0))
        assert ok is False
        assert reason == REASON_OUTSIDE_RTH

    def test_post_rth_is_blocked(self) -> None:
        gate = SessionGate()
        # 15:30 CT is 30min after RTH close
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 15, 30))
        assert ok is False
        assert reason == REASON_OUTSIDE_RTH

    def test_exactly_at_open_is_allowed(self) -> None:
        """08:30 CT is the first valid bar."""
        gate = SessionGate()
        ok, _ = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 8, 30))
        assert ok is True

    def test_exactly_at_close_is_blocked(self) -> None:
        """15:00 CT: end-exclusive -> outside RTH."""
        gate = SessionGate()
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 15, 0))
        assert ok is False
        assert reason == REASON_OUTSIDE_RTH


# --------------------------------------------------------------------------- #
# EoD cutoff
# --------------------------------------------------------------------------- #
class TestEodCutoff:
    def test_entries_blocked_at_cutoff(self) -> None:
        gate = SessionGate()
        # 15:59 CT is the cutoff; entries blocked from this bar onward
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 15, 59))
        # RTH ends at 15:00 by default -> outside_rth dominates. Make a
        # narrow window so EoD cutoff is the primary trip.
        narrow = SessionGateConfig(
            rth_start_local=time(8, 30),
            rth_end_local=time(16, 0),  # later RTH end
            eod_cutoff_local=time(15, 59),
        )
        gate2 = SessionGate(config=narrow)
        ok, reason = gate2.entries_allowed(_ct_to_utc(2026, 5, 15, 15, 59))
        assert ok is False
        assert reason == REASON_EOD_CUTOFF

    def test_entries_allowed_one_minute_before_cutoff(self) -> None:
        narrow = SessionGateConfig(
            rth_start_local=time(8, 30),
            rth_end_local=time(16, 0),
            eod_cutoff_local=time(15, 59),
        )
        gate = SessionGate(config=narrow)
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 15, 58))
        assert ok is True
        assert reason == REASON_ALLOWED

    def test_should_flatten_eod_fires_at_cutoff(self) -> None:
        narrow = SessionGateConfig(
            rth_start_local=time(8, 30),
            rth_end_local=time(16, 0),
            eod_cutoff_local=time(15, 59),
        )
        gate = SessionGate(config=narrow)
        fl, reason = gate.should_flatten_eod(
            _ct_to_utc(2026, 5, 15, 15, 59),
        )
        assert fl is True
        assert reason == REASON_EOD_PENDING

    def test_should_flatten_eod_quiet_mid_day(self) -> None:
        gate = SessionGate()
        fl, reason = gate.should_flatten_eod(
            _ct_to_utc(2026, 5, 15, 10, 0),
        )
        assert fl is False
        assert reason == REASON_EOD_NOT_DUE

    def test_should_flatten_eod_quiet_outside_rth(self) -> None:
        gate = SessionGate()
        fl, reason = gate.should_flatten_eod(
            _ct_to_utc(2026, 5, 15, 20, 0),  # past close
        )
        assert fl is False
        # Outside RTH: no flatten needed (no positions should be open).
        assert reason == REASON_EOD_NOT_DUE


# --------------------------------------------------------------------------- #
# News blackout
# --------------------------------------------------------------------------- #
class TestNewsBlackout:
    def test_entry_blocked_inside_news_window(self) -> None:
        cal = EventsCalendar(
            events=[
                CalendarEvent(
                    tag="FOMC",
                    scheduled_utc=_ct_to_utc(2026, 5, 15, 13, 0),  # 13:00 CT
                    impact="high",
                ),
            ]
        )
        gate = SessionGate(calendar=cal)
        # 12:50 CT is inside the 15-min pre-blackout window
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 12, 50))
        assert ok is False
        assert reason.startswith(REASON_NEWS_BLACKOUT)
        assert "FOMC" in reason

    def test_entry_allowed_outside_news_window(self) -> None:
        cal = EventsCalendar(
            events=[
                CalendarEvent(
                    tag="FOMC",
                    scheduled_utc=_ct_to_utc(2026, 5, 15, 13, 0),
                    impact="high",
                ),
            ]
        )
        gate = SessionGate(calendar=cal)
        # 12:00 CT is 60min before event -- outside blackout
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 12, 0))
        assert ok is True
        assert reason == REASON_ALLOWED

    def test_medium_impact_event_does_not_block(self) -> None:
        cal = EventsCalendar(
            events=[
                CalendarEvent(
                    tag="RETAIL_SALES",
                    scheduled_utc=_ct_to_utc(2026, 5, 15, 13, 0),
                    impact="medium",  # below high-impact threshold
                ),
            ]
        )
        gate = SessionGate(calendar=cal)
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 12, 55))
        assert ok is True
        assert reason == REASON_ALLOWED

    def test_news_not_enforced_when_disabled(self) -> None:
        cal = EventsCalendar(
            events=[
                CalendarEvent(
                    tag="FOMC",
                    scheduled_utc=_ct_to_utc(2026, 5, 15, 13, 0),
                    impact="high",
                ),
            ]
        )
        cfg = SessionGateConfig(block_entries_during_news=False)
        gate = SessionGate(config=cfg, calendar=cal)
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 12, 50))
        assert ok is True
        assert reason == REASON_ALLOWED


# --------------------------------------------------------------------------- #
# Naive-datetime handling
# --------------------------------------------------------------------------- #
class TestNaiveDatetimes:
    def test_naive_datetime_is_treated_as_utc(self) -> None:
        gate = SessionGate()
        naive = datetime(2026, 5, 15, 15, 0)  # 15:00 UTC == 10:00 CT CDT
        ok, _ = gate.entries_allowed(naive)
        assert ok is True


# --------------------------------------------------------------------------- #
# Precedence -- outside_rth > eod_cutoff > news
# --------------------------------------------------------------------------- #
class TestPrecedence:
    def test_outside_rth_beats_news(self) -> None:
        """If we're outside RTH the gate returns outside_rth, not a news
        tag; reporting 'news blackout' for a weekend bar would be silly.
        """
        cal = EventsCalendar(
            events=[
                CalendarEvent(
                    tag="FOMC",
                    scheduled_utc=_ct_to_utc(2026, 5, 15, 6, 0),  # pre-RTH
                    impact="high",
                ),
            ]
        )
        gate = SessionGate(calendar=cal)
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 6, 0))
        assert ok is False
        assert reason == REASON_OUTSIDE_RTH

    def test_eod_cutoff_beats_news(self) -> None:
        cal = EventsCalendar(
            events=[
                CalendarEvent(
                    tag="FED_SPEAK",
                    scheduled_utc=_ct_to_utc(2026, 5, 15, 16, 0),
                    impact="high",
                ),
            ]
        )
        cfg = SessionGateConfig(
            rth_end_local=time(16, 0),
            eod_cutoff_local=time(15, 59),
        )
        gate = SessionGate(config=cfg, calendar=cal)
        ok, reason = gate.entries_allowed(_ct_to_utc(2026, 5, 15, 15, 59))
        assert ok is False
        # EoD check runs before news check
        assert reason == REASON_EOD_CUTOFF
