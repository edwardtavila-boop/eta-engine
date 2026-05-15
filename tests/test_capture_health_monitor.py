"""Smoke + unit tests for capture_health_monitor.

Covers:
- _check_capture_file: MISSING / FRESH / STALE / TOO_SMALL paths
- _check_subscription_audit_age: NEVER_RUN / FRESH / STALE / FAIL paths
- _emit_alert: writes JSONL line (best-effort, no exception on read-only fs)
- main(): exit-code mapping (GREEN→0, YELLOW→1, RED→2)
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from eta_engine.scripts import capture_health_monitor as chm


@pytest.fixture()
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Redirect TICKS_DIR / DEPTH_DIR / LOG_DIR / SUB_STATUS_LOG into tmp."""
    ticks = tmp_path / "ticks"
    depth = tmp_path / "depth"
    logs = tmp_path / "logs"
    ticks.mkdir()
    depth.mkdir()
    logs.mkdir()
    monkeypatch.setattr(chm, "TICKS_DIR", ticks)
    monkeypatch.setattr(chm, "DEPTH_DIR", depth)
    monkeypatch.setattr(chm, "LOG_DIR", logs)
    monkeypatch.setattr(chm, "HEALTH_LOG", logs / "capture_health.jsonl")
    monkeypatch.setattr(chm, "ALERT_LOG", logs / "alerts_log.jsonl")
    monkeypatch.setattr(chm, "SUB_STATUS_LOG", logs / "ibkr_subscription_status.jsonl")
    monkeypatch.setattr(chm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(chm, "TICK_STATUS_FILE", tmp_path / "state" / "capture_tick_status.json")
    return {"ticks": ticks, "depth": depth, "logs": logs}


def _write_capture(path: Path, size: int, mtime_offset_seconds: int = 0) -> None:
    path.write_bytes(b"x" * size)
    if mtime_offset_seconds:
        new_time = time.time() + mtime_offset_seconds
        import os

        os.utime(path, (new_time, new_time))


def _write_depth_jsonl(path: Path, *, bid_levels: int = 5, ask_levels: int = 5, repeat: int = 12000) -> None:
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": "MNQ",
        "bids": [{"price": 100.0 - i, "size": 1 + i, "mm": "CME"} for i in range(bid_levels)],
        "asks": [{"price": 100.25 + i, "size": 1 + i, "mm": "CME"} for i in range(ask_levels)],
    }
    line = (json.dumps(payload) + "\n").encode("utf-8")
    path.write_bytes(line * repeat)


def test_default_depth_symbols_match_vps_capture_rotation() -> None:
    assert chm.DEFAULT_TICK_SYMBOLS == ["MNQ", "NQ", "M2K", "6E", "MCL", "MYM", "NG", "MBT"]
    assert chm.DEFAULT_DEPTH_SYMBOLS == ["MNQ", "NQ", "ES", "M2K", "MYM", "6E", "MBT"]


# ── _check_capture_file ───────────────────────────────────────────


def test_check_capture_file_missing(isolated_dirs: dict) -> None:
    today = date.today()
    out = chm._check_capture_file(isolated_dirs["ticks"], "MNQ", today, 1800, 10_000)
    assert out["today_status"] == "MISSING"
    assert out["yesterday_status"] == "MISSING"


def test_check_capture_file_fresh(isolated_dirs: dict) -> None:
    today = date.today()
    p = isolated_dirs["ticks"] / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    _write_capture(p, 50_000)
    out = chm._check_capture_file(isolated_dirs["ticks"], "MNQ", today, 1800, 10_000)
    assert out["today_status"] == "FRESH"
    assert out["today_size_bytes"] == 50_000


def test_check_capture_file_stale(isolated_dirs: dict) -> None:
    today = date.today()
    p = isolated_dirs["ticks"] / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    _write_capture(p, 50_000, mtime_offset_seconds=-3600)  # 1h old
    out = chm._check_capture_file(isolated_dirs["ticks"], "MNQ", today, 1800, 10_000)  # 30min stale threshold
    assert out["today_status"] == "STALE"
    assert out["today_mtime_age_seconds"] >= 3000


def test_check_capture_file_yesterday_too_small(isolated_dirs: dict) -> None:
    today = date.today()
    yest = today - timedelta(days=1)
    p = isolated_dirs["ticks"] / f"MNQ_{yest.strftime('%Y%m%d')}.jsonl"
    _write_capture(p, 500)  # under 10k threshold
    out = chm._check_capture_file(isolated_dirs["ticks"], "MNQ", today, 1800, 10_000)
    assert out["yesterday_status"] == "TOO_SMALL"


def test_check_capture_file_yesterday_ok(isolated_dirs: dict) -> None:
    today = date.today()
    yest = today - timedelta(days=1)
    p = isolated_dirs["ticks"] / f"MNQ_{yest.strftime('%Y%m%d')}.jsonl"
    _write_capture(p, 50_000)
    out = chm._check_capture_file(isolated_dirs["ticks"], "MNQ", today, 1800, 10_000)
    assert out["yesterday_status"] == "OK"


# ── _check_subscription_audit_age ─────────────────────────────────


def test_sub_audit_never_run(isolated_dirs: dict) -> None:
    out = chm._check_subscription_audit_age()
    assert out["status"] == "NEVER_RUN"


def test_sub_audit_fresh_passing(isolated_dirs: dict) -> None:
    rec = {"ts": datetime.now(UTC).isoformat(), "all_realtime": True}
    chm.SUB_STATUS_LOG.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    out = chm._check_subscription_audit_age()
    assert out["status"] == "FRESH"
    assert out["all_realtime"] is True


def test_sub_audit_fresh_failing(isolated_dirs: dict) -> None:
    rec = {"ts": datetime.now(UTC).isoformat(), "all_realtime": False}
    chm.SUB_STATUS_LOG.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    out = chm._check_subscription_audit_age()
    assert out["status"] == "FRESH"
    assert out["all_realtime"] is False


def test_sub_audit_stale(isolated_dirs: dict) -> None:
    old = datetime.now(UTC) - timedelta(hours=48)
    rec = {"ts": old.isoformat(), "all_realtime": True}
    chm.SUB_STATUS_LOG.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    out = chm._check_subscription_audit_age()
    assert out["status"] == "STALE"
    assert out["age_hours"] > 24


def test_sub_audit_corrupt(isolated_dirs: dict) -> None:
    chm.SUB_STATUS_LOG.write_text("not-json", encoding="utf-8")
    out = chm._check_subscription_audit_age()
    assert out["status"] == "PARSE_ERROR"


def test_tick_daemon_status_blocked(isolated_dirs: dict) -> None:
    chm.TICK_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    chm.TICK_STATUS_FILE.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "blocked_reason": {
                    "code": 10189,
                    "summary": (
                        "Tick-by-tick data blocked because another trading TWS session is connected "
                        "from a different IP address."
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    out = chm._check_tick_daemon_status()
    assert out["status"] == "BLOCKED"
    assert out["blocked_reason"]["code"] == 10189


def test_depth_book_quality_empty_book(isolated_dirs: dict) -> None:
    today = date.today()
    p = isolated_dirs["depth"] / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    p.write_text(
        json.dumps({"ts": datetime.now(UTC).isoformat(), "symbol": "MNQ", "bids": [], "asks": []}) + "\n",
        encoding="utf-8",
    )
    out = chm._check_depth_book_quality("MNQ", today)
    assert out["today_book_status"] == "EMPTY_BOOK"
    assert out["today_bid_levels"] == 0
    assert out["today_ask_levels"] == 0


# ── _emit_alert ───────────────────────────────────────────────────


def test_emit_alert_writes_line(isolated_dirs: dict) -> None:
    chm._emit_alert("YELLOW", "test alert", {"foo": "bar"})
    assert chm.ALERT_LOG.exists()
    line = chm.ALERT_LOG.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["level"] == "YELLOW"
    assert rec["source"] == "capture_health_monitor"
    assert rec["payload"] == {"foo": "bar"}


# ── main() exit-code mapping ──────────────────────────────────────


def test_main_green_when_all_fresh(isolated_dirs: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(UTC).date()
    for sym in ["MNQ", "NQ"]:
        for offset_day in [0, -1]:
            ds = today + timedelta(days=offset_day)
            _write_capture(isolated_dirs["ticks"] / f"{sym}_{ds.strftime('%Y%m%d')}.jsonl", 50_000)
            _write_depth_jsonl(isolated_dirs["depth"] / f"{sym}_{ds.strftime('%Y%m%d')}.jsonl")
    rec = {"ts": datetime.now(UTC).isoformat(), "all_realtime": True}
    chm.SUB_STATUS_LOG.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    monkeypatch.setattr("sys.argv", ["capture_health_monitor", "--symbols", "MNQ", "NQ"])
    rc = chm.main()
    assert rc == 0


def test_main_red_when_today_missing(isolated_dirs: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = {"ts": datetime.now(UTC).isoformat(), "all_realtime": True}
    chm.SUB_STATUS_LOG.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["capture_health_monitor", "--symbols", "MNQ"])
    rc = chm.main()
    assert rc == 2  # RED — today's file MISSING


def test_main_yellow_when_only_stale(isolated_dirs: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(UTC).date()
    for d, size in [(isolated_dirs["ticks"], 50_000), (isolated_dirs["depth"], 2_000_000)]:
        # Today's file STALE (1h old vs 30min threshold for ticks, 5min for depth)
        _write_capture(d / f"MNQ_{today.strftime('%Y%m%d')}.jsonl", size, mtime_offset_seconds=-7200)
        # Yesterday OK
        yest = today - timedelta(days=1)
        _write_capture(d / f"MNQ_{yest.strftime('%Y%m%d')}.jsonl", size)
    rec = {"ts": datetime.now(UTC).isoformat(), "all_realtime": True}
    chm.SUB_STATUS_LOG.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["capture_health_monitor", "--symbols", "MNQ"])
    rc = chm.main()
    assert rc == 1  # YELLOW — files exist but stale


def test_main_red_when_tick_daemon_blocked(isolated_dirs: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = {"ts": datetime.now(UTC).isoformat(), "all_realtime": True}
    chm.SUB_STATUS_LOG.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    chm.TICK_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    chm.TICK_STATUS_FILE.write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "blocked_reason": {
                    "code": 10189,
                    "summary": (
                        "Tick-by-tick data blocked because another trading TWS session is connected "
                        "from a different IP address."
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["capture_health_monitor", "--symbols", "MNQ"])
    rc = chm.main()
    assert rc == 2


def test_main_yellow_when_depth_book_empty(isolated_dirs: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(UTC).date()
    yest = today - timedelta(days=1)
    _write_capture(isolated_dirs["ticks"] / f"MNQ_{today.strftime('%Y%m%d')}.jsonl", 50_000)
    _write_capture(isolated_dirs["ticks"] / f"MNQ_{yest.strftime('%Y%m%d')}.jsonl", 50_000)
    depth_today = isolated_dirs["depth"] / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    depth_today.write_text(
        json.dumps({"ts": datetime.now(UTC).isoformat(), "symbol": "MNQ", "bids": [], "asks": []}) + "\n",
        encoding="utf-8",
    )
    _write_capture(isolated_dirs["depth"] / f"MNQ_{yest.strftime('%Y%m%d')}.jsonl", 2_000_000)
    rec = {"ts": datetime.now(UTC).isoformat(), "all_realtime": True}
    chm.SUB_STATUS_LOG.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["capture_health_monitor", "--symbols", "MNQ"])
    rc = chm.main()
    assert rc == 1
