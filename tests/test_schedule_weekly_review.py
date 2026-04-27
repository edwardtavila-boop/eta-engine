"""Tests for scripts.schedule_weekly_review — cadence guard."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from eta_engine.scripts import schedule_weekly_review as mod

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

ET = ZoneInfo("America/New_York")


def test_within_window_hits_sunday_at_20_00():
    now = datetime(2026, 4, 19, 20, 0, tzinfo=ET)  # 2026-04-19 is a Sunday
    assert mod._within_window(now) is True


def test_within_window_accepts_30_min_early():
    now = datetime(2026, 4, 19, 19, 35, tzinfo=ET)
    assert mod._within_window(now) is True


def test_within_window_accepts_30_min_late():
    now = datetime(2026, 4, 19, 20, 25, tzinfo=ET)
    assert mod._within_window(now) is True


def test_within_window_rejects_monday():
    now = datetime(2026, 4, 20, 20, 0, tzinfo=ET)  # Monday
    assert mod._within_window(now) is False


def test_within_window_rejects_sunday_far_off():
    now = datetime(2026, 4, 19, 10, 0, tzinfo=ET)  # Sunday 10am
    assert mod._within_window(now) is False


def test_should_force_reengage_on_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "preflight_dryrun_report.json").write_text(
        json.dumps({"overall": "ABORT"}),
    )
    force, reason = mod._should_force_firm_reengage()
    assert force is True
    assert "preflight" in reason.lower()


def test_should_force_reengage_on_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "preflight_dryrun_report.json").write_text(
        json.dumps({"overall": "GO"}),
    )
    (tmp_path / "docs" / "kill_log.json").write_text(
        json.dumps({"meta": {}, "entries": [{"id": 1}]}),
    )
    (tmp_path / "docs" / "weekly_review_latest.json").write_text(
        json.dumps({"kill_log_entries_at_time": 1}),
    )
    force, reason = mod._should_force_firm_reengage()
    assert force is False


def test_should_force_reengage_on_new_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "preflight_dryrun_report.json").write_text(
        json.dumps({"overall": "GO"}),
    )
    # Kill log has 4 entries, last review saw 1
    (tmp_path / "docs" / "kill_log.json").write_text(
        json.dumps({"meta": {}, "entries": [{"id": i} for i in range(4)]}),
    )
    (tmp_path / "docs" / "weekly_review_latest.json").write_text(
        json.dumps({"kill_log_entries_at_time": 1}),
    )
    force, reason = mod._should_force_firm_reengage()
    assert force is True
    assert "1 -> 4" in reason or "1 -> 4" in reason.replace("->", " -> ")


def test_emit_cron_includes_sunday_2000():
    out = mod.emit_cron()
    assert "0 20 * * 0" in out
    assert "schedule_weekly_review" in out


def test_emit_task_scheduler_includes_sunday_2000():
    out = mod.emit_task_scheduler()
    assert "SUN" in out
    assert "20:00" in out
    assert "schtasks" in out
