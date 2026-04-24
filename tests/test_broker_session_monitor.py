"""Coverage for scripts._broker_session_monitor.

Classifier, artifact writer, dedupe logic, CLI exit codes.
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from apex_predator.scripts import _broker_session_monitor as mon
from apex_predator.venues.base import ConnectionStatus, VenueConnectionReport

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _report(
    *,
    status: ConnectionStatus,
    creds: bool = True,
    error: str = "",
    details: dict[str, Any] | None = None,
    venue: str = "ibkr",
) -> VenueConnectionReport:
    return VenueConnectionReport(
        venue=venue,
        status=status,
        creds_present=creds,
        details=dict(details or {}),
        error=error,
    )


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

class TestClassify:
    def test_ready_is_green(self) -> None:
        level, reason = mon.classify(_report(status=ConnectionStatus.READY))
        assert level == "GREEN"
        assert reason == "READY"

    def test_stubbed_is_yellow(self) -> None:
        level, reason = mon.classify(
            _report(status=ConnectionStatus.STUBBED, creds=False, error="missing IBKR_ACCOUNT_ID"),
        )
        assert level == "YELLOW"
        assert "IBKR_ACCOUNT_ID" in reason

    def test_degraded_is_yellow(self) -> None:
        level, reason = mon.classify(
            _report(status=ConnectionStatus.DEGRADED, error="slow endpoint"),
        )
        assert level == "YELLOW"
        assert "slow" in reason

    def test_failed_is_red(self) -> None:
        level, reason = mon.classify(
            _report(status=ConnectionStatus.FAILED, error="HTTP 500"),
        )
        assert level == "RED"
        assert "500" in reason

    def test_unavailable_is_red(self) -> None:
        level, _ = mon.classify(_report(status=ConnectionStatus.UNAVAILABLE))
        assert level == "RED"

    def test_stubbed_fallback_reason_when_no_error(self) -> None:
        level, reason = mon.classify(
            _report(status=ConnectionStatus.STUBBED, creds=False),
        )
        assert level == "YELLOW"
        assert reason  # non-empty fallback


# ---------------------------------------------------------------------------
# write_status_file()
# ---------------------------------------------------------------------------

class TestWriteStatusFile:
    def test_writes_json_with_expected_fields(self, tmp_path: Path) -> None:
        report = _report(
            venue="ibkr",
            status=ConnectionStatus.READY,
            details={"mode": "paper", "endpoint": "https://127.0.0.1:5000/v1/api"},
        )
        out = mon.write_status_file(
            "ibkr", report, "GREEN", "READY", status_dir=tmp_path,
        )
        assert out.exists()
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["broker"] == "ibkr"
        assert payload["level"] == "GREEN"
        assert payload["reason"] == "READY"
        assert payload["status"] == "READY"
        assert payload["creds_present"] is True
        assert payload["details"]["mode"] == "paper"
        assert "generated_at_utc" in payload

    def test_creates_status_dir_if_missing(self, tmp_path: Path) -> None:
        subdir = tmp_path / "nested" / "docs"
        assert not subdir.exists()
        report = _report(status=ConnectionStatus.READY)
        mon.write_status_file(
            "tastytrade", report, "GREEN", "READY", status_dir=subdir,
        )
        assert subdir.exists()
        assert (subdir / "tastytrade_session_status.json").exists()


# ---------------------------------------------------------------------------
# append_alert() + dedupe
# ---------------------------------------------------------------------------

class TestAppendAlert:
    def test_appends_yellow_line(self, tmp_path: Path) -> None:
        alerts = tmp_path / "alerts.jsonl"
        wrote = mon.append_alert(
            "ibkr", "YELLOW", "creds missing",
            alerts_path=alerts,
            now_ts=1_000_000.0,
            dedupe_h=20.0,
        )
        assert wrote is True
        lines = alerts.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["level"] == "YELLOW"
        assert row["event"] == "broker_session_health"
        assert row["payload"]["broker"] == "ibkr"
        assert row["payload"]["reason"] == "creds missing"
        assert row["ts"] == pytest.approx(1_000_000.0)

    def test_dedupes_same_severity_within_window(self, tmp_path: Path) -> None:
        alerts = tmp_path / "alerts.jsonl"
        now = 1_000_000.0
        first = mon.append_alert(
            "ibkr", "YELLOW", "creds missing",
            alerts_path=alerts, now_ts=now, dedupe_h=20.0,
        )
        second = mon.append_alert(
            "ibkr", "YELLOW", "creds missing again",
            alerts_path=alerts, now_ts=now + 3600.0, dedupe_h=20.0,
        )
        assert first is True
        assert second is False
        lines = alerts.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1

    def test_does_not_dedupe_across_brokers(self, tmp_path: Path) -> None:
        alerts = tmp_path / "alerts.jsonl"
        now = 1_000_000.0
        mon.append_alert(
            "ibkr", "YELLOW", "x",
            alerts_path=alerts, now_ts=now, dedupe_h=20.0,
        )
        wrote = mon.append_alert(
            "tastytrade", "YELLOW", "x",
            alerts_path=alerts, now_ts=now + 60.0, dedupe_h=20.0,
        )
        assert wrote is True
        assert len(alerts.read_text(encoding="utf-8").splitlines()) == 2

    def test_escalation_red_after_yellow_is_not_suppressed(self, tmp_path: Path) -> None:
        alerts = tmp_path / "alerts.jsonl"
        now = 1_000_000.0
        mon.append_alert(
            "ibkr", "YELLOW", "creds",
            alerts_path=alerts, now_ts=now, dedupe_h=20.0,
        )
        wrote = mon.append_alert(
            "ibkr", "RED", "hard fail",
            alerts_path=alerts, now_ts=now + 60.0, dedupe_h=20.0,
        )
        assert wrote is True
        lines = alerts.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[-1])["level"] == "RED"

    def test_dedupe_window_elapses_after_cutoff(self, tmp_path: Path) -> None:
        alerts = tmp_path / "alerts.jsonl"
        now = 1_000_000.0
        mon.append_alert(
            "ibkr", "YELLOW", "x",
            alerts_path=alerts, now_ts=now, dedupe_h=1.0,
        )
        # 70 minutes later -- outside the 1h window
        wrote = mon.append_alert(
            "ibkr", "YELLOW", "x",
            alerts_path=alerts, now_ts=now + 70 * 60.0, dedupe_h=1.0,
        )
        assert wrote is True
        assert len(alerts.read_text(encoding="utf-8").splitlines()) == 2

    def test_dedupe_h_zero_disables_dedupe(self, tmp_path: Path) -> None:
        alerts = tmp_path / "alerts.jsonl"
        mon.append_alert(
            "ibkr", "YELLOW", "x",
            alerts_path=alerts, now_ts=time.time(), dedupe_h=0.0,
        )
        wrote = mon.append_alert(
            "ibkr", "YELLOW", "x",
            alerts_path=alerts, now_ts=time.time(), dedupe_h=0.0,
        )
        assert wrote is True
        assert len(alerts.read_text(encoding="utf-8").splitlines()) == 2


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------

class TestMainCLI:
    def _patch_probe(self, monkeypatch: pytest.MonkeyPatch, report: VenueConnectionReport) -> None:
        async def _fake_probe(_broker: str) -> VenueConnectionReport:
            return report
        monkeypatch.setattr(mon, "probe", _fake_probe)

    def test_ready_exits_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._patch_probe(monkeypatch, _report(status=ConnectionStatus.READY))
        rc = mon.main([
            "--broker", "ibkr",
            "--status-dir", str(tmp_path),
            "--alerts-log", str(tmp_path / "alerts.jsonl"),
        ])
        assert rc == 0
        assert "GREEN" in capsys.readouterr().out
        # No alert written when GREEN
        assert not (tmp_path / "alerts.jsonl").exists()

    def test_stubbed_exits_1_and_writes_alert(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        self._patch_probe(
            monkeypatch,
            _report(status=ConnectionStatus.STUBBED, creds=False, error="missing creds"),
        )
        alerts = tmp_path / "alerts.jsonl"
        rc = mon.main([
            "--broker", "ibkr",
            "--status-dir", str(tmp_path),
            "--alerts-log", str(alerts),
        ])
        assert rc == 1
        assert alerts.exists()
        row = json.loads(alerts.read_text(encoding="utf-8").splitlines()[0])
        assert row["level"] == "YELLOW"

    def test_failed_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        self._patch_probe(
            monkeypatch,
            _report(status=ConnectionStatus.FAILED, error="500"),
        )
        rc = mon.main([
            "--broker", "tastytrade",
            "--status-dir", str(tmp_path),
            "--alerts-log", str(tmp_path / "alerts.jsonl"),
        ])
        assert rc == 2

    def test_no_alerts_flag_skips_alert_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        self._patch_probe(
            monkeypatch,
            _report(status=ConnectionStatus.FAILED, error="x"),
        )
        alerts = tmp_path / "alerts.jsonl"
        rc = mon.main([
            "--broker", "tastytrade",
            "--status-dir", str(tmp_path),
            "--alerts-log", str(alerts),
            "--no-alerts",
        ])
        assert rc == 2
        assert not alerts.exists()

    def test_probe_crash_returns_red(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        async def _raising(_broker: str) -> VenueConnectionReport:
            raise RuntimeError("boom")
        monkeypatch.setattr(mon, "probe", _raising)
        rc = mon.main([
            "--broker", "ibkr",
            "--status-dir", str(tmp_path),
            "--alerts-log", str(tmp_path / "alerts.jsonl"),
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "RED" in err
        assert "boom" in err

    def test_rejects_dormant_broker(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            mon.main([
                "--broker", "tradovate",
                "--status-dir", str(tmp_path),
            ])
