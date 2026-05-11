from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.scripts import tick_anomaly_detector as detector


def _valid_tick(price: float = 100.0, size: float = 2.0) -> dict:
    ts = datetime(2026, 5, 11, 17, tzinfo=UTC)
    return {
        "ts": ts.isoformat(),
        "epoch_s": ts.timestamp(),
        "symbol": "MNQ1",
        "price": price,
        "size": size,
    }


def test_validate_tick_accepts_clean_print() -> None:
    result = detector.validate_tick(_valid_tick(), last_real_price=99.75)

    assert result.verdict == "OK"
    assert result.anomalies == []


def test_validate_tick_warns_on_zero_size() -> None:
    result = detector.validate_tick(_valid_tick(size=0))

    assert result.verdict == "WARN"
    assert result.anomalies == ["zero_size"]


def test_validate_tick_skips_implausible_jump() -> None:
    result = detector.validate_tick(
        _valid_tick(price=130.0),
        last_real_price=100.0,
        max_price_jump_pct=10.0,
    )

    assert result.verdict == "SKIP"
    assert any(item.startswith("implausible_jump") for item in result.anomalies)


def test_audit_tick_file_aggregates_bad_json_and_warnings(tmp_path) -> None:
    path = tmp_path / "MNQ_20260511.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_valid_tick(price=100.0)),
                "{bad json",
                json.dumps(_valid_tick(price=100.25, size=0)),
                json.dumps(_valid_tick(price=130.0)),
            ]
        ),
        encoding="utf-8",
    )

    summary = detector.audit_tick_file(path, max_emit=0)

    assert summary["n_total"] == 4
    assert summary["n_ok"] == 1
    assert summary["n_warn"] == 1
    assert summary["n_skip"] == 2
    assert summary["anomaly_counts"]["bad_json"] == 1
    assert summary["anomaly_counts"]["zero_size"] == 1
    assert summary["anomaly_counts"]["implausible_jump"] == 1
