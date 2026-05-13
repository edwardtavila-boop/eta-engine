"""Tests for the supercharge Phase 6 additions:

- depth_anomaly_detector: per-snap validator (crossed book, NaN, etc.)
- l2_drift_monitor: rolling-window performance drift
- l2_equity_simulator: Monte Carlo equity curves
- l2_portfolio_limits: cross-strategy concurrency limiter
"""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import (
    depth_anomaly_detector as anom,
)
from eta_engine.scripts import (
    l2_drift_monitor as drift,
)
from eta_engine.scripts import (
    l2_equity_simulator as eq,
)
from eta_engine.strategies import (
    l2_portfolio_limits as plim,
)

# ────────────────────────────────────────────────────────────────────
# depth_anomaly_detector
# ────────────────────────────────────────────────────────────────────


def _good_snap() -> dict:
    return {
        "ts": "2026-05-11T14:30:00+00:00",
        "epoch_s": datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC).timestamp(),
        "symbol": "MNQ",
        "bids": [
            {"price": 100.0, "size": 10, "mm": "CME"},
            {"price": 99.75, "size": 8, "mm": "CME"},
            {"price": 99.50, "size": 6, "mm": "CME"},
        ],
        "asks": [
            {"price": 100.25, "size": 10, "mm": "CME"},
            {"price": 100.50, "size": 8, "mm": "CME"},
            {"price": 100.75, "size": 6, "mm": "CME"},
        ],
        "spread": 0.25,
        "mid": 100.125,
    }


def test_anomaly_clean_snapshot_returns_ok() -> None:
    result = anom.validate_snapshot(_good_snap())
    assert result.verdict == "OK"
    assert result.anomalies == []


def test_anomaly_crossed_book_returns_skip() -> None:
    snap = _good_snap()
    snap["bids"][0]["price"] = 101.0  # higher than best ask
    result = anom.validate_snapshot(snap)
    assert result.verdict == "SKIP"
    assert any("crossed_book" in a for a in result.anomalies)


def test_anomaly_missing_fields_fail_close() -> None:
    snap = _good_snap()
    del snap["bids"]
    result = anom.validate_snapshot(snap)
    assert result.verdict == "FAIL_CLOSE"


def test_anomaly_empty_side_skips() -> None:
    snap = _good_snap()
    snap["bids"] = []
    result = anom.validate_snapshot(snap)
    assert result.verdict == "SKIP"


def test_anomaly_negative_spread_skips() -> None:
    snap = _good_snap()
    snap["spread"] = -0.25
    result = anom.validate_snapshot(snap)
    assert result.verdict == "SKIP"


def test_anomaly_nan_price_skips() -> None:
    snap = _good_snap()
    snap["bids"][0]["price"] = float("nan")
    result = anom.validate_snapshot(snap)
    assert result.verdict == "SKIP"


def test_anomaly_negative_size_skips() -> None:
    snap = _good_snap()
    snap["bids"][0]["size"] = -5
    result = anom.validate_snapshot(snap)
    assert result.verdict == "SKIP"


def test_anomaly_mid_outside_nbbo_warns() -> None:
    snap = _good_snap()
    snap["mid"] = 200.0  # way outside
    result = anom.validate_snapshot(snap)
    assert result.verdict == "WARN"
    assert any("mid_outside_nbbo" in a for a in result.anomalies)


def test_anomaly_duplicate_bid_prices_warns() -> None:
    snap = _good_snap()
    snap["bids"][1]["price"] = snap["bids"][0]["price"]
    result = anom.validate_snapshot(snap)
    assert result.verdict == "WARN"
    assert any("duplicate_bid" in a for a in result.anomalies)


def test_anomaly_audit_file_aggregates(tmp_path: Path) -> None:
    path = tmp_path / "MNQ_20260511.jsonl"
    snaps = [_good_snap()] * 5
    bad = _good_snap()
    bad["bids"][0]["price"] = 101.0  # crossed
    snaps.append(bad)
    path.write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")
    summary = anom.audit_capture_file(path)
    assert summary["n_total"] == 6
    assert summary["n_ok"] == 5
    assert summary["n_skip"] == 1


# ────────────────────────────────────────────────────────────────────
# l2_drift_monitor
# ────────────────────────────────────────────────────────────────────


def test_drift_no_records_insufficient(tmp_path: Path) -> None:
    report = drift.compute_drift(
        "book_imbalance",
        "MNQ",
        _path=tmp_path / "nonexistent.jsonl",
    )
    assert report.drift_verdict == "INSUFFICIENT"


def test_drift_ok_when_ratio_above_1(tmp_path: Path) -> None:
    """Initial sharpe 0.5, current 0.6 → ratio 1.2 → OK."""
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    records = [
        # Initial valid record from 60 days ago
        {
            "ts": (now - timedelta(days=60)).isoformat(),
            "strategy": "book_imbalance",
            "symbol": "MNQ",
            "sharpe_proxy": 0.5,
            "sharpe_proxy_valid": True,
            "win_rate": 0.55,
            "n_trades": 30,
        },
        # Recent record (within last 14d)
        {
            "ts": (now - timedelta(days=2)).isoformat(),
            "strategy": "book_imbalance",
            "symbol": "MNQ",
            "sharpe_proxy": 0.6,
            "sharpe_proxy_valid": True,
            "win_rate": 0.58,
            "n_trades": 35,
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    report = drift.compute_drift("book_imbalance", "MNQ", _path=path)
    assert report.drift_verdict == "OK"
    assert report.sharpe_ratio is not None
    assert report.sharpe_ratio > 1.0


def test_drift_critical_when_edge_collapses(tmp_path: Path) -> None:
    """Initial 1.5, current 0.3 → ratio 0.2 → CRITICAL."""
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    records = [
        {
            "ts": (now - timedelta(days=60)).isoformat(),
            "strategy": "book_imbalance",
            "symbol": "MNQ",
            "sharpe_proxy": 1.5,
            "sharpe_proxy_valid": True,
            "win_rate": 0.70,
            "n_trades": 30,
        },
        {
            "ts": (now - timedelta(days=2)).isoformat(),
            "strategy": "book_imbalance",
            "symbol": "MNQ",
            "sharpe_proxy": 0.3,
            "sharpe_proxy_valid": True,
            "win_rate": 0.52,
            "n_trades": 30,
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    report = drift.compute_drift("book_imbalance", "MNQ", _path=path)
    assert report.drift_verdict == "CRITICAL"


# ────────────────────────────────────────────────────────────────────
# l2_equity_simulator
# ────────────────────────────────────────────────────────────────────


def test_equity_sim_no_returns() -> None:
    report = eq.simulate([])
    assert report.median_end_equity_usd is None
    assert "no historical" in report.notes[0]


def test_equity_sim_positive_edge_grows_account() -> None:
    """+1R / -0.5R 60% win-rate → positive expectancy → median ends positive."""
    returns = [1.0] * 60 + [-0.5] * 40  # 60% win, +1 / -0.5
    report = eq.simulate(returns, n_paths=1000, trades_per_path=100, starting_equity_usd=10000.0, seed=42)
    assert report.median_end_equity_usd is not None
    assert report.median_end_equity_usd > 10000.0  # positive expectancy


def test_equity_sim_negative_edge_drains_account() -> None:
    """-1R / +0.5R 60% loss-rate → negative expectancy → median ends down."""
    returns = [-1.0] * 60 + [0.5] * 40
    report = eq.simulate(returns, n_paths=1000, trades_per_path=100, starting_equity_usd=10000.0, seed=42)
    assert report.median_end_equity_usd is not None
    # Expected drift ≈ -40 per trade → -40*100 = -4000
    assert report.median_end_equity_usd < 10000.0


def test_equity_sim_drawdown_reported() -> None:
    returns = [1.0, -1.0, 1.0, -1.0]  # break-even, all drawdown
    report = eq.simulate(returns, n_paths=100, trades_per_path=20, seed=42)
    assert report.median_max_drawdown_pct is not None
    assert report.median_max_drawdown_pct > 0


def test_equity_sim_risk_of_ruin_zero_with_strong_edge() -> None:
    """All wins, no losses → never goes to zero."""
    returns = [10.0] * 100
    report = eq.simulate(returns, n_paths=100, trades_per_path=10, starting_equity_usd=1000.0, seed=42)
    assert report.risk_of_ruin == 0.0


def test_equity_sim_risk_of_ruin_high_with_big_losers() -> None:
    """Small account, large losses → some paths go to zero."""
    returns = [-100.0] * 50 + [50.0] * 50  # massive negative expectancy + tail risk
    report = eq.simulate(returns, n_paths=1000, trades_per_path=100, starting_equity_usd=500.0, seed=42)
    assert report.risk_of_ruin is not None
    assert report.risk_of_ruin > 0.5  # most paths get ruined


# ────────────────────────────────────────────────────────────────────
# l2_portfolio_limits
# ────────────────────────────────────────────────────────────────────


def test_portfolio_no_positions_passes(tmp_path: Path) -> None:
    decision = plim.check_portfolio_limits(
        symbol="MNQ",
        side="LONG",
        qty=1,
        _fill_path=tmp_path / "empty.jsonl",
        _log_path=tmp_path / "log.jsonl",
    )
    assert decision.blocked is False
    assert decision.reason == "ok"


def test_portfolio_blocks_same_side_stacking(tmp_path: Path) -> None:
    """Already have 1 LONG MNQ open → 2nd LONG on same symbol blocked."""
    fill_path = tmp_path / "fill.jsonl"
    now = datetime.now(UTC)
    # Open LONG MNQ entry, not exited
    fill_path.write_text(
        json.dumps(
            {
                "ts": now.isoformat(),
                "signal_id": "MNQ-LONG-1",
                "broker_exec_id": "x1",
                "exit_reason": "ENTRY",
                "side": "LONG",
                "actual_fill_price": 100.0,
                "qty_filled": 1,
                "symbol": "MNQ",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decision = plim.check_portfolio_limits(
        symbol="MNQ",
        side="LONG",
        qty=1,
        _fill_path=fill_path,
        _log_path=tmp_path / "log.jsonl",
        max_same_side_per_symbol=1,
    )
    assert decision.blocked is True
    assert "same_side_stacking" in decision.reason


def test_portfolio_blocks_total_absolute_exceeded(tmp_path: Path) -> None:
    """5 open contracts across symbols → 6th blocked at default 5."""
    fill_path = tmp_path / "fill.jsonl"
    now = datetime.now(UTC)
    lines = []
    for i in range(5):
        lines.append(
            json.dumps(
                {
                    "ts": now.isoformat(),
                    "signal_id": f"SYM{i}-LONG-1",
                    "broker_exec_id": f"x{i}",
                    "exit_reason": "ENTRY",
                    "side": "LONG",
                    "actual_fill_price": 100.0,
                    "qty_filled": 1,
                    "symbol": f"SYM{i}",  # distinct symbol per fill
                }
            )
        )
    fill_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    decision = plim.check_portfolio_limits(
        symbol="SYM_NEW",
        side="LONG",
        qty=1,
        _fill_path=fill_path,
        _log_path=tmp_path / "log.jsonl",
        max_total_absolute_contracts=5,
        max_same_side_per_symbol=1,
        max_concurrent_per_symbol=2,
    )
    assert decision.blocked is True
    assert "total_absolute_exceeded" in decision.reason


def test_portfolio_allows_after_exit(tmp_path: Path) -> None:
    """ENTRY + matching exit → net = 0 → next entry allowed."""
    fill_path = tmp_path / "fill.jsonl"
    now = datetime.now(UTC)
    lines = [
        json.dumps(
            {
                "ts": (now - timedelta(seconds=60)).isoformat(),
                "signal_id": "MNQ-LONG-old",
                "broker_exec_id": "x1",
                "exit_reason": "ENTRY",
                "side": "LONG",
                "actual_fill_price": 100.0,
                "qty_filled": 1,
                "symbol": "MNQ",
            }
        ),
        # Exit the position
        json.dumps(
            {
                "ts": now.isoformat(),
                "signal_id": "MNQ-LONG-old",
                "broker_exec_id": "x2",
                "exit_reason": "TARGET",
                "side": "LONG",
                "actual_fill_price": 102.0,
                "qty_filled": 1,
                "symbol": "MNQ",
            }
        ),
    ]
    fill_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    decision = plim.check_portfolio_limits(
        symbol="MNQ",
        side="LONG",
        qty=1,
        _fill_path=fill_path,
        _log_path=tmp_path / "log.jsonl",
    )
    assert decision.blocked is False
