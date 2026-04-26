"""Tests for ``strategies.shadow_paper_tracker``.

The tracker's window-streak logic is exercised end-to-end via the
chaos drill (``scripts.chaos_drills.shadow_paper_tracker_drill``);
this module covers the per-method contract + the journal-sink wiring
that feeds the SHADOW_TICK avenger handler.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from apex_predator.strategies.shadow_paper_tracker import ShadowPaperTracker

if TYPE_CHECKING:
    from pathlib import Path


class TestShadowPaperTrackerJournal:
    def test_no_journal_path_no_writes(self, tmp_path: Path) -> None:
        """The default constructor must not write anywhere."""
        tracker = ShadowPaperTracker()
        tracker.record_shadow_trade(
            "alpha", "TREND", pnl_r=1.2, is_win=True,
        )
        assert list(tmp_path.iterdir()) == []

    def test_journal_writes_one_line_per_trade(self, tmp_path: Path) -> None:
        journal = tmp_path / "shadow.jsonl"
        tracker = ShadowPaperTracker(journal_path=journal)
        tracker.record_shadow_trade("alpha", "TREND", pnl_r=1.2, is_win=True)
        tracker.record_shadow_trade("alpha", "TREND", pnl_r=-0.5, is_win=False)
        tracker.record_shadow_trade("beta",  "RANGE", pnl_r=0.3, is_win=True)

        lines = journal.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3

        rows = [json.loads(line) for line in lines]
        # Schema pin -- handler reads these fields.
        for r in rows:
            assert {"ts", "strategy", "regime", "pnl_r", "is_win"} <= set(r)
        assert rows[0]["strategy"] == "alpha"
        assert rows[0]["pnl_r"] == 1.2
        assert rows[0]["is_win"] is True
        assert rows[1]["is_win"] is False
        assert rows[2]["regime"] == "RANGE"

    def test_journal_failure_degrades_to_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unwriteable journal path must not break record_shadow_trade."""
        # Point at a path under a file (so .parent.mkdir succeeds but
        # opening the file raises). On Linux this surfaces as
        # NotADirectoryError -> OSError.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        journal = blocker / "shadow.jsonl"  # parent is a file

        tracker = ShadowPaperTracker(journal_path=journal)
        with caplog.at_level("WARNING"):
            tracker.record_shadow_trade(
                "alpha", "TREND", pnl_r=1.0, is_win=True,
            )
        assert any(
            "shadow_paper_tracker journal write failed" in rec.message
            for rec in caplog.records
        )

    def test_journal_compatible_with_handler(self, tmp_path: Path) -> None:
        """Round-trip: tracker writes -> handler reads + tallies."""
        from apex_predator.brain.avengers import local_handlers as lh
        from apex_predator.brain.avengers.dispatch import BackgroundTask

        journal = tmp_path / "shadow.jsonl"
        tracker = ShadowPaperTracker(journal_path=journal)
        tracker.record_shadow_trade("alpha", "TREND", pnl_r=1.2, is_win=True)
        tracker.record_shadow_trade("alpha", "TREND", pnl_r=-0.4, is_win=False)
        tracker.record_shadow_trade("alpha", "TREND", pnl_r=0.8, is_win=True)

        # Point the handler at the same journal.
        import os
        os.environ["APEX_SHADOW_JOURNAL_PATH"] = str(journal)
        try:
            result = lh._shadow_tick_handler(BackgroundTask.SHADOW_TICK)
        finally:
            os.environ.pop("APEX_SHADOW_JOURNAL_PATH", None)

        assert result is not None
        assert result["parsed"] == 3
        assert result["buckets"] == 1
        bucket = result["by_bucket"]["alpha::TREND"]
        assert bucket["n"] == 3
        assert bucket["win_rate"] == pytest.approx(2 / 3)
        assert bucket["cum_r"] == pytest.approx(1.6)
