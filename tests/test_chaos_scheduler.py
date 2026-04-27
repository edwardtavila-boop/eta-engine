"""Tests for ``eta_engine.chaos.scheduler``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 -- runtime via tmp_path

from eta_engine.chaos.scheduler import (
    SESSION_BLACKOUT_END,
    SESSION_BLACKOUT_START,
    ChaosScheduleEntry,
    ChaosScheduler,
    make_default_schedule,
)


def test_due_during_blackout_returns_empty(tmp_path: Path) -> None:
    s = ChaosScheduler(
        schedule=[ChaosScheduleEntry("chrony_kill", every_days=30)],
        state_path=tmp_path / "state.json",
    )
    # Pick a time that's clearly inside the blackout (15:00 UTC).
    now = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
    assert s.in_session_blackout(now) is True
    assert s.due_drills(now) == []


def test_due_outside_blackout_returns_first_drill(tmp_path: Path) -> None:
    s = ChaosScheduler(
        schedule=[
            ChaosScheduleEntry("a", every_days=30),
            ChaosScheduleEntry("b", every_days=30),
        ],
        state_path=tmp_path / "state.json",
    )
    # 03:00 UTC -- well outside blackout.
    now = datetime(2026, 4, 27, 3, 0, tzinfo=UTC)
    due = s.due_drills(now)
    assert len(due) == 1     # capped to one per tick
    assert due[0].drill_name == "a"


def test_drill_not_due_within_window(tmp_path: Path) -> None:
    s = ChaosScheduler(
        schedule=[ChaosScheduleEntry("a", every_days=30)],
        state_path=tmp_path / "state.json",
    )
    now = datetime(2026, 4, 27, 3, 0, tzinfo=UTC)
    s.mark_run("a", now)
    # Same day -- not yet due again.
    assert s.due_drills(now) == []
    assert s.due_drills(now + timedelta(days=10)) == []
    assert len(s.due_drills(now + timedelta(days=31))) == 1


def test_mark_run_persists_across_instances(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    s1 = ChaosScheduler(
        schedule=[ChaosScheduleEntry("x", every_days=30)],
        state_path=p,
    )
    now = datetime(2026, 4, 27, 3, 0, tzinfo=UTC)
    s1.mark_run("x", now)

    s2 = ChaosScheduler(
        schedule=[ChaosScheduleEntry("x", every_days=30)],
        state_path=p,
    )
    assert "x" in s2.last_run
    assert s2.due_drills(now) == []


def test_in_session_blackout_at_boundaries() -> None:
    s = ChaosScheduler(state_path=Path("/tmp/_unused.json"))
    # Exactly at start should be blackout.
    edge_start = datetime.combine(
        datetime(2026, 4, 27, tzinfo=UTC).date(),
        SESSION_BLACKOUT_START,
        tzinfo=UTC,
    )
    edge_end = datetime.combine(
        datetime(2026, 4, 27, tzinfo=UTC).date(),
        SESSION_BLACKOUT_END,
        tzinfo=UTC,
    )
    assert s.in_session_blackout(edge_start) is True
    assert s.in_session_blackout(edge_end) is True
    # Outside the window.
    assert s.in_session_blackout(datetime(2026, 4, 27, 6, 0, tzinfo=UTC)) is False
    assert s.in_session_blackout(datetime(2026, 4, 27, 22, 0, tzinfo=UTC)) is False


def test_default_schedule_has_no_high_severity() -> None:
    schedule = make_default_schedule()
    assert all(e.severity_max != "high" for e in schedule)
    assert len(schedule) >= 4


def test_corrupt_state_file_does_not_crash(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("{not json")
    s = ChaosScheduler(
        schedule=[ChaosScheduleEntry("z", every_days=30)],
        state_path=p,
    )
    # Empty last_run -> drill is due.
    now = datetime(2026, 4, 27, 3, 0, tzinfo=UTC)
    assert len(s.due_drills(now)) == 1
