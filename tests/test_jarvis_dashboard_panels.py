"""Wired-panel tests for ``scripts.jarvis_dashboard``.

Pins the contracts for the 6 panels that now read real subsystem
state (breaker, deadman, forecast, daemons, promotion, calibration).
The drift card is covered separately in
``test_jarvis_hardening.py::TestDashboardDriftPanel``.
"""
from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Breaker
# ---------------------------------------------------------------------------

class TestRenderBreaker:
    def test_no_data_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        monkeypatch.setattr(mod, "BREAKER_PATH", tmp_path / "missing.json")
        out = mod._render_breaker()
        assert out["state"] == "NO_DATA"
        assert out["tripped_at"] is None
        assert "missing.json" in out["path"]

    def test_reads_state_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        breaker = tmp_path / "breaker.json"
        breaker.write_text(json.dumps({
            "state": "OPEN",
            "tripped_at": "2026-04-26T12:00:00Z",
            "trip_reason": "5 consecutive failures",
            "consecutive_failures": 5,
        }))
        monkeypatch.setattr(mod, "BREAKER_PATH", breaker)
        out = mod._render_breaker()
        assert out["state"] == "OPEN"
        assert out["tripped_at"] == "2026-04-26T12:00:00Z"
        assert out["trip_reason"] == "5 consecutive failures"
        assert out["consecutive_failures"] == 5

    def test_corrupt_file_safe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        breaker = tmp_path / "breaker.json"
        breaker.write_text("{not json")
        monkeypatch.setattr(mod, "BREAKER_PATH", breaker)
        out = mod._render_breaker()
        assert out["state"] == "CORRUPT"
        assert "error" in out


# ---------------------------------------------------------------------------
# Deadman
# ---------------------------------------------------------------------------

class TestRenderDeadman:
    def test_no_data_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        monkeypatch.setattr(mod, "DEADMAN_PATH", tmp_path / "missing.jsonl")
        out = mod._render_deadman()
        assert out["last_heartbeat"] is None
        assert out["stale_seconds"] is None

    def test_reads_last_heartbeat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        journal = tmp_path / "heartbeat.jsonl"
        recent_ts = time.time() - 30.0  # 30 seconds ago
        rows = [
            {"kind": "heartbeat", "persona": "ROBIN",  "ts": recent_ts - 60},
            {"kind": "heartbeat", "persona": "ALFRED", "ts": recent_ts - 30},
            {"kind": "heartbeat", "persona": "BATMAN", "ts": recent_ts},
        ]
        journal.write_text(
            "\n".join(json.dumps(r) for r in rows), encoding="utf-8",
        )
        monkeypatch.setattr(mod, "DEADMAN_PATH", journal)
        out = mod._render_deadman()
        assert out["last_persona"] == "BATMAN"
        assert out["last_heartbeat"] == pytest.approx(recent_ts)
        # stale ~30s, generously bound for CI jitter.
        assert 0.0 <= out["stale_seconds"] <= 60.0


# ---------------------------------------------------------------------------
# Daemons
# ---------------------------------------------------------------------------

class TestRenderDaemons:
    def test_all_down_when_pid_dir_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        monkeypatch.setattr(mod, "JARVIS_PID_DIR", tmp_path / "empty")
        out = mod._render_daemons()
        assert out["healthy"] == []
        assert set(out["down"]) == {"BATMAN", "ALFRED", "ROBIN", "JARVIS"}

    def test_alive_pid_marks_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        pid_dir = tmp_path / ".jarvis"
        pid_dir.mkdir()
        # Use the test runner's PID -- guaranteed alive for the duration.
        my_pid = os.getpid()
        (pid_dir / "daemon_robin.pid").write_text(str(my_pid))
        monkeypatch.setattr(mod, "JARVIS_PID_DIR", pid_dir)
        out = mod._render_daemons()
        assert "ROBIN" in out["healthy"]
        # Other personas have no PID file -> down.
        assert {"BATMAN", "ALFRED", "JARVIS"} <= set(out["down"])

    def test_dead_pid_marks_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        pid_dir = tmp_path / ".jarvis"
        pid_dir.mkdir()
        # PID 1 exists on POSIX (init); use a deliberately bogus one.
        (pid_dir / "daemon_alfred.pid").write_text("999999999")
        monkeypatch.setattr(mod, "JARVIS_PID_DIR", pid_dir)
        out = mod._render_daemons()
        assert "ALFRED" in out["down"]


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

class TestRenderPromotion:
    def test_no_data_when_journal_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        monkeypatch.setattr(mod, "PROMOTION_PATH", tmp_path / "missing.jsonl")
        out = mod._render_promotion()
        assert out["in_flight"] == []

    def test_returns_last_5_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        journal = tmp_path / "promotion.jsonl"
        rows = [
            {"strategy_id": f"strat_{i}", "from_stage": "SHADOW",
             "to_stage": "PAPER", "action": "PROMOTE", "ts": f"2026-04-{i:02d}"}
            for i in range(1, 8)
        ]
        journal.write_text(
            "\n".join(json.dumps(r) for r in rows), encoding="utf-8",
        )
        monkeypatch.setattr(mod, "PROMOTION_PATH", journal)
        out = mod._render_promotion()
        assert len(out["in_flight"]) == 5
        # Last entries kept (most recent first in tail order).
        assert out["in_flight"][-1]["strategy_id"] == "strat_7"


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

class TestRenderCalibration:
    def test_no_data_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        monkeypatch.setattr(mod, "CALIBRATION_PATH", tmp_path / "missing.jsonl")
        out = mod._render_calibration()
        assert out["last_run"] is None
        assert out["ks_pvalue"] is None

    def test_reads_last_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        journal = tmp_path / "calibration.jsonl"
        journal.write_text(json.dumps({
            "ts": "2026-04-26T12:00:00Z",
            "ks_pvalue": 0.42,
            "verdict": "PASS",
        }) + "\n", encoding="utf-8")
        monkeypatch.setattr(mod, "CALIBRATION_PATH", journal)
        out = mod._render_calibration()
        assert out["last_run"] == "2026-04-26T12:00:00Z"
        assert out["ks_pvalue"] == 0.42
        assert out["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# Forecast (now wired -- reads ~/.jarvis/forecast.jsonl)
# ---------------------------------------------------------------------------

class TestRenderForecast:
    def test_no_data_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        monkeypatch.setattr(mod, "FORECAST_PATH", tmp_path / "missing.jsonl")
        out = mod._render_forecast()
        assert out["status"] == "no_data"
        assert out["horizon_minutes"] is None
        assert out["confidence"] is None

    def test_reads_last_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        journal = tmp_path / "forecast.jsonl"
        journal.write_text(json.dumps({
            "ts": "2026-04-26T12:00:00Z",
            "level": "ELEVATED",
            "level_raw": 0.48,
            "trend": "UP",
            "trend_raw": 0.02,
            "forecast_1": 0.50,
            "forecast_3": 0.54,
            "forecast_5": 0.58,
            "samples": 30,
            "note": "trending up",
        }) + "\n", encoding="utf-8")
        monkeypatch.setattr(mod, "FORECAST_PATH", journal)
        out = mod._render_forecast()
        assert out["status"] == "ok"
        assert out["level"] == "ELEVATED"
        assert out["trend"] == "UP"
        assert out["horizon_minutes"] == 5
        assert out["forecast_5"] == 0.58
        assert out["samples"] == 30
        assert out["note"] == "trending up"

    def test_confidence_band_high_when_trend_steady(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        journal = tmp_path / "forecast.jsonl"
        journal.write_text(json.dumps({
            "level": "NORMAL", "trend": "FLAT", "trend_raw": 0.001,
            "forecast_5": 0.30, "samples": 50,
        }) + "\n", encoding="utf-8")
        monkeypatch.setattr(mod, "FORECAST_PATH", journal)
        assert mod._render_forecast()["confidence"] == "high"

    def test_confidence_band_low_when_trend_swings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        journal = tmp_path / "forecast.jsonl"
        journal.write_text(json.dumps({
            "level": "HIGH", "trend": "UP", "trend_raw": 0.08,
            "forecast_5": 0.95, "samples": 12,
        }) + "\n", encoding="utf-8")
        monkeypatch.setattr(mod, "FORECAST_PATH", journal)
        assert mod._render_forecast()["confidence"] == "low"


# ---------------------------------------------------------------------------
# collect_state still includes all panels post-wiring
# ---------------------------------------------------------------------------

class TestCollectStateSurface:
    def test_all_panels_present(self) -> None:
        from apex_predator.scripts import jarvis_dashboard as mod
        state = mod.collect_state()
        for key in (
            "drift", "breaker", "deadman", "forecast", "daemons",
            "promotion", "calibration", "journal", "alerts",
        ):
            assert key in state, f"panel {key} missing from collect_state()"
            assert isinstance(state[key], dict)
