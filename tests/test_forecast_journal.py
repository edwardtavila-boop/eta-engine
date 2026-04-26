"""Tests for ``brain.jarvis_v3.forecast_journal``.

The appender is the producer side of the forecast panel surface --
the dashboard reader is covered by
``test_jarvis_dashboard_panels.TestRenderForecast``. Keep this
module focused on the JSONL writer + level/trend banding.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from apex_predator.brain.jarvis_v3 import forecast_journal as fj
from apex_predator.brain.jarvis_v3.predictive import Projection

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _projection(level: float = 0.5, trend: float = 0.02) -> Projection:
    return Projection(
        level=level,
        trend=trend,
        forecast_1=min(1.0, level + trend),
        forecast_3=min(1.0, level + 3 * trend),
        forecast_5=min(1.0, level + 5 * trend),
        samples=20,
        note="test",
    )


class TestLevelBand:
    def test_normal(self) -> None:
        assert fj._level_band(0.0) == "NORMAL"
        assert fj._level_band(0.39) == "NORMAL"

    def test_elevated(self) -> None:
        assert fj._level_band(0.40) == "ELEVATED"
        assert fj._level_band(0.54) == "ELEVATED"

    def test_high(self) -> None:
        assert fj._level_band(0.55) == "HIGH"
        assert fj._level_band(0.74) == "HIGH"

    def test_extreme(self) -> None:
        assert fj._level_band(0.75) == "EXTREME"
        assert fj._level_band(1.00) == "EXTREME"


class TestTrendBand:
    def test_flat_within_default_band(self) -> None:
        assert fj._trend_band(0.0) == "FLAT"
        assert fj._trend_band(0.004) == "FLAT"
        assert fj._trend_band(-0.004) == "FLAT"

    def test_up(self) -> None:
        assert fj._trend_band(0.01) == "UP"

    def test_down(self) -> None:
        assert fj._trend_band(-0.01) == "DOWN"


class TestRecordProjection:
    def test_writes_one_jsonl_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        journal = tmp_path / "forecast.jsonl"
        monkeypatch.setenv("APEX_FORECAST_JOURNAL", str(journal))
        ok = fj.record_projection(_projection(level=0.45, trend=0.02))
        assert ok is True
        assert journal.exists()
        lines = journal.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert {"ts", "level", "trend", "trend_raw", "forecast_1",
                "forecast_3", "forecast_5", "samples", "note"} <= set(rec)
        assert rec["level"] == "ELEVATED"
        assert rec["trend"] == "UP"
        assert rec["samples"] == 20

    def test_appends_one_line_per_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        journal = tmp_path / "forecast.jsonl"
        monkeypatch.setenv("APEX_FORECAST_JOURNAL", str(journal))
        for level in (0.30, 0.50, 0.80):
            fj.record_projection(_projection(level=level, trend=0.01))
        rows = [
            json.loads(line)
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]
        assert [r["level"] for r in rows] == ["NORMAL", "ELEVATED", "EXTREME"]

    def test_unwritable_path_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Parent path is a file, so .parent.mkdir succeeds but open
        # raises NotADirectoryError -> OSError.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        journal = blocker / "forecast.jsonl"
        monkeypatch.setenv("APEX_FORECAST_JOURNAL", str(journal))
        with caplog.at_level("WARNING"):
            ok = fj.record_projection(_projection())
        assert ok is False
        assert any(
            "forecast_journal append failed" in r.message
            for r in caplog.records
        )

    def test_default_path_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("APEX_FORECAST_JOURNAL", raising=False)
        # Re-anchor the default to tmp so we don't write to real ~/.jarvis.
        monkeypatch.setattr(
            fj, "DEFAULT_FORECAST_JOURNAL", tmp_path / "default.jsonl",
        )
        ok = fj.record_projection(_projection())
        assert ok is True
        assert (tmp_path / "default.jsonl").exists()
