"""Tests for the supercharge Phase 9 additions (Category 2 closure):

- l2_risk_metrics (Sortino + Calmar + daily rollup)
- l2_per_symbol_ensemble (per-(strategy, symbol) weights)
- l2_strategy_correlation (cross-strategy pnl + signal correlation)
- l2_universe_audit (survivorship-bias check)
- l2_commission_tier_optimizer (IBKR tier projection)
- l2_strategy_versioning (config version registry)
- l2_ensemble_validator (does ensemble beat best individual?)
"""
# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import (
    l2_commission_tier_optimizer as tier,
)
from eta_engine.scripts import l2_ensemble_validator as ev
from eta_engine.scripts import l2_risk_metrics as rm
from eta_engine.scripts import l2_strategy_correlation as corr
from eta_engine.scripts import l2_strategy_versioning as ver
from eta_engine.scripts import l2_universe_audit as ua
from eta_engine.strategies import l2_per_symbol_ensemble as pse


# ────────────────────────────────────────────────────────────────────
# l2_risk_metrics
# ────────────────────────────────────────────────────────────────────


def test_sortino_higher_when_only_upside() -> None:
    """Symmetric returns sortino ≈ sharpe; all-positive returns → None."""
    sym = rm.compute_sortino([1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0])
    assert sym is not None
    assert sym == 0.0  # mean is 0
    # All wins → sortino undefined (no downside)
    all_wins = rm.compute_sortino([1.0] * 10)
    assert all_wins is None


def test_sortino_returns_none_below_min_sample() -> None:
    assert rm.compute_sortino([1.0, 2.0]) is None


def test_calmar_no_drawdown_returns_none() -> None:
    """Monotonically increasing equity → no drawdown → None."""
    eq = [1000.0, 1010.0, 1020.0, 1030.0, 1040.0, 1050.0]
    assert rm.compute_calmar(eq) is None


def test_calmar_returns_value_when_drawdown_exists() -> None:
    """Peaks at 1100, falls to 900 → 18.2% drawdown."""
    eq = [1000.0, 1100.0, 900.0, 950.0, 1000.0, 1050.0]
    calmar = rm.compute_calmar(eq)
    assert calmar is not None


def test_max_consecutive_losers_counts_streak() -> None:
    assert rm.compute_max_consecutive_losers([1.0, -1.0, -1.0, -1.0, 1.0]) == 3
    assert rm.compute_max_consecutive_losers([-1.0, 1.0, -1.0]) == 1
    assert rm.compute_max_consecutive_losers([1.0, 1.0, 1.0]) == 0


def test_max_drawdown_finds_peak_to_trough() -> None:
    eq = [1000.0, 1500.0, 800.0, 1000.0, 1500.0]
    dd_usd, dd_pct = rm.compute_max_drawdown(eq)
    assert dd_usd == 700.0  # 1500 → 800
    assert dd_pct > 46.0 and dd_pct < 47.0


def test_compute_metrics_no_data_returns_empty(tmp_path: Path) -> None:
    metrics = rm.compute_metrics(
        _signal_path=tmp_path / "sig.jsonl",
        _fill_path=tmp_path / "fill.jsonl",
    )
    assert metrics.n_trades == 0


# ────────────────────────────────────────────────────────────────────
# l2_per_symbol_ensemble
# ────────────────────────────────────────────────────────────────────


@dataclass
class _StubSig:
    strategy_id: str
    side: str
    confidence: float
    signal_id: str = "stub"


def test_per_symbol_weights_split_by_pair(tmp_path: Path) -> None:
    """Same strategy on two symbols can have different weights."""
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    records = [
        {"ts": now.isoformat(), "strategy": "book_imbalance", "symbol": "MNQ",
          "sharpe_proxy": 0.8, "sharpe_proxy_valid": True},
        {"ts": now.isoformat(), "strategy": "book_imbalance", "symbol": "GC",
          "sharpe_proxy": -0.3, "sharpe_proxy_valid": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                     encoding="utf-8")
    weights = pse.compute_per_symbol_weights(_path=path)
    assert ("book_imbalance", "MNQ") in weights.weights
    assert weights.weights[("book_imbalance", "MNQ")] == 0.8
    # Negative sharpe gets floored to 0
    assert weights.weights[("book_imbalance", "GC")] == 0.0


def test_per_symbol_vote_uses_pair_weight(tmp_path: Path) -> None:
    weights = pse.PerSymbolWeights(
        weights={
            ("book_imbalance", "MNQ"): 1.0,
            ("microprice_drift", "MNQ"): 0.5,
        },
        global_fallback={"book_imbalance": 0.3},
        ts=datetime.now(UTC).isoformat(),
    )
    signals = [
        _StubSig(strategy_id="book_imbalance", side="LONG", confidence=0.8),
        _StubSig(strategy_id="microprice_drift", side="LONG", confidence=0.7),
    ]
    out = pse.vote_per_symbol(signals, weights, symbol="MNQ",
                                ensemble_threshold=0.5)
    assert out is not None
    assert out.side == "LONG"


def test_per_symbol_vote_falls_back_to_global(tmp_path: Path) -> None:
    """No (strategy, ES) pair → fall back to strategy's global weight."""
    weights = pse.PerSymbolWeights(
        weights={},  # no per-symbol data
        global_fallback={"book_imbalance": 0.8},
        ts=datetime.now(UTC).isoformat(),
    )
    signals = [
        _StubSig(strategy_id="book_imbalance", side="LONG", confidence=0.8),
    ]
    out = pse.vote_per_symbol(signals, weights, symbol="ES",
                                ensemble_threshold=0.5)
    assert out is not None
    # Constituent should be tagged as global_fallback
    assert out.constituent_signals[0]["weight_source"] == "global_fallback"


# ────────────────────────────────────────────────────────────────────
# l2_strategy_correlation
# ────────────────────────────────────────────────────────────────────


def test_pearson_perfect_correlation() -> None:
    import pytest
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]
    p = corr._pearson(xs, ys)
    assert p == pytest.approx(1.0, abs=1e-10)


def test_pearson_negative_correlation() -> None:
    import pytest
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [5.0, 4.0, 3.0, 2.0, 1.0]
    p = corr._pearson(xs, ys)
    assert p == pytest.approx(-1.0, abs=1e-10)


def test_pearson_below_min_sample_returns_none() -> None:
    assert corr._pearson([1.0, 2.0], [1.0, 2.0]) is None


def test_correlations_with_no_data(tmp_path: Path) -> None:
    report = corr.compute_correlations(
        _signal_path=tmp_path / "sig.jsonl",
        _fill_path=tmp_path / "fill.jsonl",
    )
    assert report.n_strategies == 0


# ────────────────────────────────────────────────────────────────────
# l2_universe_audit
# ────────────────────────────────────────────────────────────────────


def test_universe_audit_no_data(tmp_path: Path) -> None:
    report = ua.run_audit(_backtest_path=tmp_path / "bt.jsonl")
    assert report.n_findings == 0


def test_universe_audit_flags_unsupported_symbol(tmp_path: Path) -> None:
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    path.write_text(json.dumps({
        "ts": now.isoformat(),
        "strategy": "book_imbalance",
        "symbol": "FAKE_SYMBOL_NOT_IN_SPECS",
    }) + "\n", encoding="utf-8")
    report = ua.run_audit(_backtest_path=path)
    assert report.n_unsupported >= 1
    assert any(f.finding == "UNSUPPORTED_SYMBOL" for f in report.findings)


def test_universe_audit_passes_for_known_symbol(tmp_path: Path) -> None:
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    path.write_text(json.dumps({
        "ts": now.isoformat(),
        "strategy": "book_imbalance",
        "symbol": "MNQ",  # in SYMBOL_SPECS + capture + registry
    }) + "\n", encoding="utf-8")
    report = ua.run_audit(_backtest_path=path)
    assert any(f.finding == "OK" for f in report.findings)


# ────────────────────────────────────────────────────────────────────
# l2_commission_tier_optimizer
# ────────────────────────────────────────────────────────────────────


def test_tier_classifier_buckets_correctly() -> None:
    assert tier._tier_for_monthly(500)[0] == 0      # tier 1
    assert tier._tier_for_monthly(5_000)[0] == 1    # tier 2
    assert tier._tier_for_monthly(15_000)[0] == 2   # tier 3
    assert tier._tier_for_monthly(50_000)[0] == 3   # tier 4


def test_tier_no_fills_returns_zero(tmp_path: Path) -> None:
    proj = tier.compute_tier_projection(
        _fill_path=tmp_path / "fill.jsonl")
    assert proj.n_fills_recent_days == 0
    assert proj.monthly_projected_fills == 0.0


def test_tier_projects_monthly_from_recent(tmp_path: Path) -> None:
    fill_path = tmp_path / "fill.jsonl"
    now = datetime.now(UTC)
    # 10 fills in last 5 days → projected 60/month
    lines = []
    for i in range(10):
        lines.append(json.dumps({
            "ts": (now - timedelta(days=i // 2)).isoformat(),
            "qty_filled": 1,
            "signal_id": f"s{i}",
        }))
    fill_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proj = tier.compute_tier_projection(since_days=5, _fill_path=fill_path)
    assert proj.n_fills_recent_days == 10
    # 10 * (30/5) = 60
    assert proj.monthly_projected_fills == 60.0


# ────────────────────────────────────────────────────────────────────
# l2_strategy_versioning
# ────────────────────────────────────────────────────────────────────


def test_versioning_register_new_version(tmp_path: Path) -> None:
    path = tmp_path / "versions.json"
    v = ver.register_version(
        "book_imbalance", "v1",
        {"entry_threshold": 1.75, "consecutive_snaps": 3},
        rationale="initial",
        _path=path,
    )
    assert v.version == "v1"
    assert v.config_hash != ""
    # Re-read
    reg = ver.load_registry(_path=path)
    assert "book_imbalance" in reg.versions
    assert len(reg.versions["book_imbalance"]) == 1


def test_versioning_new_version_closes_prior(tmp_path: Path) -> None:
    path = tmp_path / "versions.json"
    ver.register_version(
        "book_imbalance", "v1",
        {"entry_threshold": 1.75},
        _path=path,
    )
    # v2 should close v1's effective_to
    ver.register_version(
        "book_imbalance", "v2",
        {"entry_threshold": 2.0},
        _path=path,
    )
    reg = ver.load_registry(_path=path)
    v1 = next(v for v in reg.versions["book_imbalance"] if v.version == "v1")
    v2 = next(v for v in reg.versions["book_imbalance"] if v.version == "v2")
    assert v1.effective_to is not None  # closed
    assert v2.effective_to is None       # active


def test_versioning_active_version(tmp_path: Path) -> None:
    path = tmp_path / "versions.json"
    ver.register_version(
        "book_imbalance", "v1",
        {"entry_threshold": 1.75},
        _path=path,
    )
    active = ver.active_version("book_imbalance", _path=path)
    assert active is not None
    assert active.version == "v1"


def test_versioning_config_hash_stable() -> None:
    h1 = ver._config_hash({"a": 1, "b": 2})
    h2 = ver._config_hash({"b": 2, "a": 1})  # different order
    assert h1 == h2  # JSON sorted keys make it stable


def test_versioning_filter_records_to_version(tmp_path: Path) -> None:
    path = tmp_path / "versions.json"
    now = datetime.now(UTC)
    ver.register_version(
        "book_imbalance", "v1",
        {"x": 1},
        effective_from=now - timedelta(days=30),
        _path=path,
    )
    ver.register_version(
        "book_imbalance", "v2",
        {"x": 2},
        effective_from=now - timedelta(days=10),
        _path=path,
    )
    records = [
        {"ts": (now - timedelta(days=20)).isoformat()},  # under v1
        {"ts": (now - timedelta(days=5)).isoformat()},   # under v2
    ]
    v1_recs = ver.filter_records_to_version(records, "book_imbalance", "v1",
                                                 _path=path)
    v2_recs = ver.filter_records_to_version(records, "book_imbalance", "v2",
                                                 _path=path)
    assert len(v1_recs) == 1
    assert len(v2_recs) == 1


# ────────────────────────────────────────────────────────────────────
# l2_ensemble_validator
# ────────────────────────────────────────────────────────────────────


def test_ensemble_validator_no_data_inconclusive(tmp_path: Path) -> None:
    report = ev.validate_ensemble(_backtest_path=tmp_path / "bt.jsonl")
    assert report.verdict == "INCONCLUSIVE"


def test_ensemble_validator_single_constituent_inconclusive(tmp_path: Path) -> None:
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    path.write_text(json.dumps({
        "ts": now.isoformat(),
        "strategy": "book_imbalance",
        "sharpe_proxy": 0.5,
        "sharpe_proxy_valid": True,
    }) + "\n", encoding="utf-8")
    report = ev.validate_ensemble(_backtest_path=path)
    assert report.n_constituents == 1
    assert report.verdict == "INCONCLUSIVE"


def test_ensemble_validator_finds_best_constituent(tmp_path: Path) -> None:
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    records = [
        {"ts": now.isoformat(), "strategy": "book_imbalance",
          "sharpe_proxy": 1.5, "sharpe_proxy_valid": True},
        {"ts": now.isoformat(), "strategy": "microprice_drift",
          "sharpe_proxy": 0.3, "sharpe_proxy_valid": True},
        {"ts": now.isoformat(), "strategy": "footprint_absorption",
          "sharpe_proxy": 0.1, "sharpe_proxy_valid": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                     encoding="utf-8")
    report = ev.validate_ensemble(_backtest_path=path)
    assert report.best_constituent == "book_imbalance"
    assert report.best_constituent_sharpe == 1.5


def test_ensemble_validator_underperform_when_one_dominates(tmp_path: Path) -> None:
    """When book_imbalance has 1.5 and others are 0.1, weighted ensemble
    is < 1.5 → UNDERPERFORM."""
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    records = [
        {"ts": now.isoformat(), "strategy": "book_imbalance",
          "sharpe_proxy": 1.5, "sharpe_proxy_valid": True},
        {"ts": now.isoformat(), "strategy": "microprice_drift",
          "sharpe_proxy": 0.1, "sharpe_proxy_valid": True},
        {"ts": now.isoformat(), "strategy": "footprint_absorption",
          "sharpe_proxy": 0.05, "sharpe_proxy_valid": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                     encoding="utf-8")
    report = ev.validate_ensemble(_backtest_path=path)
    assert report.verdict == "UNDERPERFORM"
    assert report.margin is not None
    assert report.margin < 0
