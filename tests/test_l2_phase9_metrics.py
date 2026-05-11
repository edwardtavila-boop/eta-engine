from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.scripts import l2_commission_tier_optimizer as tiers
from eta_engine.scripts import l2_risk_metrics as risk
from eta_engine.scripts import l2_strategy_correlation as corr
from eta_engine.scripts import l2_universe_audit as universe
from eta_engine.strategies import l2_per_symbol_ensemble as pse


def _jsonl(path, records: list[dict]) -> None:  # noqa: ANN001
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")


def test_commission_tier_projection_counts_recent_fills(tmp_path) -> None:
    now = datetime.now(UTC).isoformat()
    fill_path = tmp_path / "fills.jsonl"
    _jsonl(fill_path, [
        {"ts": now, "qty_filled": 10},
        {"ts": now, "qty_filled": 15},
    ])

    projection = tiers.compute_tier_projection(since_days=30, _fill_path=fill_path)

    assert projection.n_fills_recent_days == 25
    assert projection.monthly_projected_fills == 25.0
    assert projection.current_per_side_usd == 0.85


def test_risk_metric_helpers_compute_drawdown_and_loser_streak() -> None:
    assert risk.compute_max_consecutive_losers([1, -1, -2, 3, -1]) == 2
    assert risk.compute_max_drawdown([100.0, 110.0, 90.0, 95.0]) == (20.0, 18.18)
    assert risk.compute_sortino([1.0, -1.0, 2.0, -0.5, 1.5]) is not None


def test_risk_metrics_reconstructs_trade_pnl(tmp_path) -> None:
    now = datetime.now(UTC).isoformat()
    signal_path = tmp_path / "signals.jsonl"
    fill_path = tmp_path / "fills.jsonl"
    _jsonl(signal_path, [
        {"ts": now, "signal_id": "sig-1", "strategy_id": "book_imbalance", "side": "BUY"},
    ])
    _jsonl(fill_path, [
        {"ts": now, "signal_id": "sig-1", "exit_reason": "ENTRY", "actual_fill_price": 100.0},
        {"ts": now, "signal_id": "sig-1", "exit_reason": "TARGET", "actual_fill_price": 101.0},
    ])

    metrics = risk.compute_metrics(
        "book_imbalance",
        _signal_path=signal_path,
        _fill_path=fill_path,
    )

    assert metrics.n_trades == 1
    assert metrics.n_wins == 1
    assert metrics.expectancy == 2.0


def test_strategy_correlation_computes_pearson() -> None:
    assert corr._pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)
    assert corr._pearson([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None


def test_strategy_correlation_reports_pair_from_logs(tmp_path) -> None:
    base = datetime.now(UTC) - timedelta(days=6)
    signal_path = tmp_path / "signals.jsonl"
    fill_path = tmp_path / "fills.jsonl"
    signals: list[dict] = []
    fills: list[dict] = []
    for day in range(5):
        ts = (base + timedelta(days=day)).isoformat()
        for strategy, entry, exit_price in (
            ("book_imbalance", 100.0, 101.0 + day),
            ("microprice_drift", 100.0, 101.0 + day),
        ):
            sid = f"{strategy}-{day}"
            signals.append({"ts": ts, "signal_id": sid, "strategy_id": strategy, "side": "BUY"})
            fills.append({"ts": ts, "signal_id": sid, "exit_reason": "ENTRY", "actual_fill_price": entry})
            fills.append({"ts": ts, "signal_id": sid, "exit_reason": "TARGET", "actual_fill_price": exit_price})
    _jsonl(signal_path, signals)
    _jsonl(fill_path, fills)

    report = corr.compute_correlations(_signal_path=signal_path, _fill_path=fill_path)

    assert report.n_pairs == 1
    assert report.pairs[0].pnl_correlation == 1.0
    assert report.pairs[0].signal_agreement_rate == 1.0


def test_universe_audit_flags_unsupported_symbol(tmp_path, monkeypatch) -> None:
    now = datetime.now(UTC).isoformat()
    backtest_path = tmp_path / "backtests.jsonl"
    _jsonl(backtest_path, [
        {"ts": now, "strategy": "book_imbalance", "symbol": "ZZZ"},
    ])
    monkeypatch.setattr(universe, "_strategy_registry_symbols", lambda: set())
    monkeypatch.setattr(universe, "_harness_supported_symbols", lambda: {"MNQ"})
    monkeypatch.setattr(universe, "_current_capture_symbols", lambda: {"MNQ"})

    report = universe.run_audit(_backtest_path=backtest_path)

    assert report.n_unsupported == 1
    assert report.findings[0].finding == "UNSUPPORTED_SYMBOL"


@dataclass
class _Signal:
    strategy_id: str
    side: str
    confidence: float
    signal_id: str


def test_per_symbol_ensemble_prefers_symbol_specific_weight() -> None:
    signal = pse.vote_per_symbol(
        [_Signal("book_imbalance", "LONG", 0.8, "sig-a")],
        pse.PerSymbolWeights(
            weights={("book_imbalance", "MNQ"): 1.5},
            global_fallback={"book_imbalance": 0.1},
        ),
        symbol="MNQ",
        ensemble_threshold=0.5,
    )

    assert signal is not None
    assert signal.side == "LONG"
    assert signal.constituent_signals[0]["weight_source"] == "per_symbol"
