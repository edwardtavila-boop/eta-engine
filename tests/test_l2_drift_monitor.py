from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from eta_engine.scripts import l2_drift_monitor as monitor


def _record(ts: datetime, sharpe: float, n_trades: int = 40) -> dict:
    return {
        "ts": ts.isoformat(),
        "strategy": "book_imbalance",
        "symbol": "MNQ",
        "sharpe_proxy_valid": True,
        "sharpe_proxy": sharpe,
        "win_rate": 0.55,
        "n_trades": n_trades,
    }


def test_drift_verdict_thresholds() -> None:
    assert monitor._drift_verdict(None) == "INSUFFICIENT"
    assert monitor._drift_verdict(1.0) == "OK"
    assert monitor._drift_verdict(0.8) == "DEGRADING"
    assert monitor._drift_verdict(0.5) == "DRIFTING"
    assert monitor._drift_verdict(0.2) == "CRITICAL"


def test_compute_drift_flags_degrading_edge(tmp_path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "l2_backtest_runs.jsonl"
    records = [
        _record(now - timedelta(days=60), 1.0),
        _record(now - timedelta(days=10), 0.8),
        _record(now - timedelta(days=2), 0.6),
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    report = monitor.compute_drift(
        "book_imbalance",
        "MNQ",
        _path=path,
        recent_window_days=14,
        baseline_window_days=14,
    )

    assert report.initial_sharpe == 1.0
    assert report.current_rolling_sharpe == 0.7
    assert report.sharpe_ratio == 0.7
    assert report.drift_verdict == "DEGRADING"
