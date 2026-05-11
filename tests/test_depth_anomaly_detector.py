from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.scripts import depth_anomaly_detector as detector


def _valid_snapshot() -> dict:
    ts = datetime(2026, 5, 11, 17, tzinfo=UTC)
    return {
        "ts": ts.isoformat(),
        "epoch_s": ts.timestamp(),
        "bids": [{"price": 100.0, "size": 3}, {"price": 99.75, "size": 2}],
        "asks": [{"price": 100.25, "size": 4}, {"price": 100.5, "size": 2}],
        "spread": 0.25,
        "mid": 100.125,
    }


def test_validate_snapshot_accepts_clean_book() -> None:
    result = detector.validate_snapshot(_valid_snapshot())

    assert result.verdict == "OK"
    assert result.anomalies == []


def test_validate_snapshot_skips_crossed_book() -> None:
    snap = _valid_snapshot()
    snap["bids"][0]["price"] = 101.0

    result = detector.validate_snapshot(snap)

    assert result.verdict == "SKIP"
    assert any(anomaly.startswith("crossed_book") for anomaly in result.anomalies)


def test_audit_capture_file_aggregates_bad_json_and_anomalies(tmp_path) -> None:
    path = tmp_path / "MNQ_20260511.jsonl"
    crossed = _valid_snapshot()
    crossed["bids"][0]["price"] = 101.0
    path.write_text(
        "\n".join(
            [
                json.dumps(_valid_snapshot()),
                "{bad json",
                json.dumps(crossed),
            ]
        ),
        encoding="utf-8",
    )

    summary = detector.audit_capture_file(path, max_emit=0)

    assert summary["n_total"] == 3
    assert summary["n_ok"] == 1
    assert summary["n_skip"] == 1
    assert summary["n_fail_close"] == 1
    assert summary["anomaly_counts"]["bad_json"] == 1
    assert summary["anomaly_counts"]["crossed_book"] == 1
