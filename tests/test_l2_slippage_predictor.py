from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.scripts import l2_slippage_predictor as slip


def _jsonl(path, records: list[dict]) -> None:  # noqa: ANN001
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")


def test_size_and_vol_bucket_boundaries() -> None:
    assert slip._size_bucket(1) == "1"
    assert slip._size_bucket(3) == "2-5"
    assert slip._size_bucket(8) == "6-10"
    assert slip._size_bucket(11) == "10+"
    assert slip._vol_bucket(0.1) == "low"
    assert slip._vol_bucket(1.0) == "mid"
    assert slip._vol_bucket(3.0) == "high"


def test_train_model_builds_bucket_from_stop_fills(tmp_path) -> None:
    now = datetime.now(UTC)
    fill_path = tmp_path / "fills.jsonl"
    signal_path = tmp_path / "signals.jsonl"
    _jsonl(fill_path, [
        {
            "ts": now.isoformat(),
            "signal_id": "sig-1",
            "exit_reason": "STOP",
            "slip_ticks_vs_intended": 1.5,
            "qty_filled": 2,
        },
        {
            "ts": now.isoformat(),
            "signal_id": "sig-1",
            "exit_reason": "STOP",
            "slip_ticks_vs_intended": 2.5,
            "qty_filled": 2,
        },
    ])
    _jsonl(signal_path, [
        {
            "ts": now.isoformat(),
            "signal_id": "sig-1",
            "regime": "NORMAL",
            "vol_proxy": 1.0,
        },
    ])

    model = slip.train_model(_fill_path=fill_path, _signal_path=signal_path)

    assert model.n_fills == 2
    assert len(model.buckets) == 1
    assert model.buckets[0].mean_slip_ticks == 2.0
    assert slip.predict_slip(regime="NORMAL", session=model.buckets[0].session, size=2, vol=1.0, model=model) == 2.0


def test_predict_slip_falls_back_to_default_without_model() -> None:
    assert slip.predict_slip(model=None) == 1.0
