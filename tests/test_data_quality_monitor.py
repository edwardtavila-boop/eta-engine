from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

from eta_engine.core.control_plane import GATE_STATE_BLOCKED, GATE_STATE_HEALTHY, GATE_STATE_PAPER_ONLY
from eta_engine.feeds.data_quality_monitor import DataQualityMonitor


def test_missing_bar_file_maps_to_missing_quality_and_blocked_state(tmp_path) -> None:
    monitor = DataQualityMonitor(bar_dir=tmp_path, output_path=tmp_path / "feed_health.json", hermes_enabled=False)
    record = monitor._check_bar_feed("BTC", datetime.now(UTC))

    assert record["status"] == "missing"
    assert record["quality_tier"] == "missing"
    assert record["gate_state"] == GATE_STATE_BLOCKED
    assert "bar_file_missing" in record["reason_codes"]


def test_stale_bar_file_maps_to_stale_quality_and_paper_only_state(tmp_path) -> None:
    bar = tmp_path / "BTC_5m.csv"
    bar.write_text("time,open,high,low,close,volume\n", encoding="utf-8")
    stale_ts = datetime.now(UTC) - timedelta(minutes=45)
    os.utime(bar, (stale_ts.timestamp(), stale_ts.timestamp()))

    monitor = DataQualityMonitor(bar_dir=tmp_path, output_path=tmp_path / "feed_health.json", hermes_enabled=False)
    record = monitor._check_bar_feed("BTC", datetime.now(UTC))

    assert record["status"] == "stale"
    assert record["quality_tier"] == "stale"
    assert record["gate_state"] == GATE_STATE_PAPER_ONLY
    assert "feed_stale" in record["reason_codes"]


def test_run_aggregates_overall_status_from_gate_states(tmp_path, monkeypatch) -> None:
    monitor = DataQualityMonitor(bar_dir=tmp_path, output_path=tmp_path / "feed_health.json", hermes_enabled=False)
    monkeypatch.setattr(
        monitor,
        "_check_all_feeds",
        lambda: [
            {
                "feed_name": "bar_csv",
                "symbol": "BTC",
                "status": "healthy",
                "quality_tier": "live",
                "gate_state": GATE_STATE_HEALTHY,
                "reason_codes": [],
            },
            {
                "feed_name": "ibkr_tws",
                "symbol": "MNQ",
                "status": "critical",
                "quality_tier": "missing",
                "gate_state": GATE_STATE_BLOCKED,
                "reason_codes": ["ibkr_unreachable"],
            },
        ],
    )

    snapshot = monitor.run()

    assert snapshot.overall_status == "critical"
    payload = json.loads((tmp_path / "feed_health.json").read_text(encoding="utf-8"))
    assert payload["feeds"][1]["gate_state"] == GATE_STATE_BLOCKED


def test_run_writes_quality_fields_for_healthy_feed(tmp_path, monkeypatch) -> None:
    monitor = DataQualityMonitor(bar_dir=tmp_path, output_path=tmp_path / "feed_health.json", hermes_enabled=False)
    monkeypatch.setattr(
        monitor,
        "_check_all_feeds",
        lambda: [
            {
                "feed_name": "bar_csv",
                "symbol": "ETH",
                "status": "healthy",
                "quality_tier": "live",
                "gate_state": GATE_STATE_HEALTHY,
                "reason_codes": [],
            }
        ],
    )

    snapshot = monitor.run()

    assert snapshot.overall_status == "healthy"
    payload = json.loads((tmp_path / "feed_health.json").read_text(encoding="utf-8"))
    assert payload["feeds"][0]["quality_tier"] == "live"
    assert payload["feeds"][0]["gate_state"] == GATE_STATE_HEALTHY
