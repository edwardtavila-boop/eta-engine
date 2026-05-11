"""Tests for disk_space_monitor — verifies threshold mapping + alert emission."""
from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

import pytest

from eta_engine.scripts import disk_space_monitor as dsm

GB = 1024 ** 3
DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


@pytest.fixture()
def isolated_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(dsm, "LOG_DIR", tmp_path)
    monkeypatch.setattr(dsm, "HISTORY_LOG", tmp_path / "disk_space.jsonl")
    monkeypatch.setattr(dsm, "ALERT_LOG", tmp_path / "alerts_log.jsonl")
    monkeypatch.setattr(dsm, "TICKS_DIR", tmp_path / "ticks")
    monkeypatch.setattr(dsm, "DEPTH_DIR", tmp_path / "depth")
    return tmp_path


# ── _verdict_for ──────────────────────────────────────────────────


def test_verdict_thresholds() -> None:
    assert dsm._verdict_for(100.0) == "GREEN"
    assert dsm._verdict_for(50.0) == "GREEN"
    assert dsm._verdict_for(30.0) == "YELLOW"
    assert dsm._verdict_for(15.0) == "YELLOW"
    assert dsm._verdict_for(10.0) == "RED"
    assert dsm._verdict_for(5.0) == "RED"
    assert dsm._verdict_for(2.0) == "CRITICAL"
    assert dsm._verdict_for(0.5) == "CRITICAL"


# ── _stat_one ────────────────────────────────────────────────────


def test_stat_one_green(isolated_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DiskUsage(total=500 * GB, used=100 * GB, free=400 * GB)
    monkeypatch.setattr("shutil.disk_usage", lambda p: fake)
    out = dsm._stat_one("test", isolated_logs)
    assert out["verdict"] == "GREEN"
    assert out["free_gb"] == 400.0
    assert out["pct_used"] == 20.0


def test_stat_one_red(isolated_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DiskUsage(total=500 * GB, used=495 * GB, free=5 * GB)
    monkeypatch.setattr("shutil.disk_usage", lambda p: fake)
    out = dsm._stat_one("test", isolated_logs)
    assert out["verdict"] == "RED"


def test_stat_one_critical(isolated_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DiskUsage(total=500 * GB, used=499 * GB, free=1 * GB)
    monkeypatch.setattr("shutil.disk_usage", lambda p: fake)
    out = dsm._stat_one("test", isolated_logs)
    assert out["verdict"] == "CRITICAL"


def test_stat_one_oserror(isolated_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(_p: Path) -> None:
        raise OSError("no such device")
    monkeypatch.setattr("shutil.disk_usage", raise_oserror)
    out = dsm._stat_one("test", isolated_logs)
    assert out["verdict"] == "ERROR"
    assert "no such device" in out["error"]


# ── main() exit-code mapping ─────────────────────────────────────


def test_main_green_exits_zero(isolated_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DiskUsage(total=500 * GB, used=100 * GB, free=400 * GB)
    monkeypatch.setattr("shutil.disk_usage", lambda p: fake)
    monkeypatch.setattr("sys.argv", ["disk_space_monitor"])
    rc = dsm.main()
    assert rc == 0
    # Did NOT emit alert
    assert not dsm.ALERT_LOG.exists()


def test_main_yellow_exits_one(isolated_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DiskUsage(total=500 * GB, used=480 * GB, free=20 * GB)
    monkeypatch.setattr("shutil.disk_usage", lambda p: fake)
    monkeypatch.setattr("sys.argv", ["disk_space_monitor"])
    rc = dsm.main()
    assert rc == 1
    # DID emit alert
    assert dsm.ALERT_LOG.exists()
    alert = json.loads(dsm.ALERT_LOG.read_text(encoding="utf-8").strip())
    assert alert["level"] == "YELLOW"
    assert alert["source"] == "disk_space_monitor"


def test_main_critical_exits_three(isolated_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DiskUsage(total=500 * GB, used=499 * GB, free=1 * GB)
    monkeypatch.setattr("shutil.disk_usage", lambda p: fake)
    monkeypatch.setattr("sys.argv", ["disk_space_monitor"])
    rc = dsm.main()
    assert rc == 3


def test_main_writes_history_jsonl(isolated_logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DiskUsage(total=500 * GB, used=100 * GB, free=400 * GB)
    monkeypatch.setattr("shutil.disk_usage", lambda p: fake)
    monkeypatch.setattr("sys.argv", ["disk_space_monitor"])
    dsm.main()
    assert dsm.HISTORY_LOG.exists()
    rec = json.loads(dsm.HISTORY_LOG.read_text(encoding="utf-8").strip())
    assert rec["verdict"] == "GREEN"
    assert len(rec["checks"]) == 3  # ticks + depth + logs
