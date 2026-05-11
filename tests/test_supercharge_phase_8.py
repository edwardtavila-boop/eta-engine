"""Tests for the supercharge Phase 8 additions (final realistic batch):

- tick_stream_consumer + tick_anomaly_detector
- l2_cpcv (Combinatorial Purged CV)
- l2_performance_attribution
- l2_slippage_predictor
- l2_strategy_ensemble
- l2_cross_broker_arb
"""
# ruff: noqa: N802, PLR2004
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eta_engine.scripts import (
    l2_cpcv,
)
from eta_engine.scripts import (
    l2_cross_broker_arb as arb,
)
from eta_engine.scripts import l2_performance_attribution as pa
from eta_engine.scripts import l2_slippage_predictor as slip
from eta_engine.scripts import tick_anomaly_detector as tick_anom
from eta_engine.scripts import tick_stream_consumer as tick
from eta_engine.strategies import l2_strategy_ensemble as ens

# ────────────────────────────────────────────────────────────────────
# tick_stream_consumer
# ────────────────────────────────────────────────────────────────────


def _write_tick_file(path: Path, n: int = 10, start_price: float = 100.0) -> None:
    base = datetime.now(UTC) - timedelta(hours=1)
    lines = []
    for i in range(n):
        ts = base + timedelta(seconds=i)
        lines.append(json.dumps({
            "ts": ts.isoformat(),
            "epoch_s": ts.timestamp(),
            "symbol": "MNQ",
            "price": round(start_price + i * 0.25, 4),
            "size": 1,
            "exchange": "CME",
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_tick_parse_line_valid() -> None:
    line = json.dumps({
        "ts": "2026-05-11T14:30:00+00:00",
        "epoch_s": 1746950400.0,
        "symbol": "MNQ",
        "price": 29270.25,
        "size": 1,
    })
    rec = tick._parse_line(line)
    assert rec is not None
    assert rec.price == 29270.25
    assert rec.symbol == "MNQ"


def test_tick_parse_line_bad_json_returns_none() -> None:
    assert tick._parse_line("not json") is None
    assert tick._parse_line("") is None


def test_tick_iter_from_file(tmp_path: Path) -> None:
    path = tmp_path / "MNQ_20260511.jsonl"
    _write_tick_file(path, n=5)
    ticks = list(tick.iter_ticks_from_file(path))
    assert len(ticks) == 5
    assert all(t.symbol == "MNQ" for t in ticks)


def test_tick_feed_strategy_microprice(tmp_path: Path,
                                          monkeypatch) -> None:
    path = tmp_path / "MNQ_20260511.jsonl"
    _write_tick_file(path, n=20)
    monkeypatch.setattr(tick, "TICKS_DIR", tmp_path)

    received: list[float] = []

    class _StubStrategy:
        def update_trade(self, price, ts=None):  # noqa: ARG002
            received.append(price)

    n = tick.feed_strategy_microprice("MNQ", "20260511", _StubStrategy())
    assert n == 20
    assert len(received) == 20


# ────────────────────────────────────────────────────────────────────
# tick_anomaly_detector
# ────────────────────────────────────────────────────────────────────


def test_tick_anomaly_clean_tick_ok() -> None:
    result = tick_anom.validate_tick({"price": 100.0, "size": 1})
    assert result.verdict == "OK"


def test_tick_anomaly_zero_size_warns() -> None:
    result = tick_anom.validate_tick({"price": 100.0, "size": 0})
    assert result.verdict == "WARN"
    assert "zero_size" in result.anomalies


def test_tick_anomaly_nan_price_skips() -> None:
    result = tick_anom.validate_tick({"price": float("nan"), "size": 1})
    assert result.verdict == "SKIP"


def test_tick_anomaly_unreported_flag_warns() -> None:
    result = tick_anom.validate_tick(
        {"price": 100.0, "size": 1, "unreported": True})
    assert result.verdict == "WARN"
    assert "unreported_flag" in result.anomalies


def test_tick_anomaly_implausible_jump_skips() -> None:
    """20% jump from prior price → SKIP."""
    result = tick_anom.validate_tick(
        {"price": 120.0, "size": 1}, last_real_price=100.0)
    assert result.verdict == "SKIP"
    assert any("implausible_jump" in a for a in result.anomalies)


def test_tick_anomaly_audit_file(tmp_path: Path) -> None:
    path = tmp_path / "MNQ_20260511.jsonl"
    lines = [
        json.dumps({"price": 100.0, "size": 1}),  # ok
        json.dumps({"price": 100.25, "size": 1}),  # ok
        json.dumps({"price": 100.25, "size": 0}),  # warn (zero size)
        json.dumps({"price": -1, "size": 1}),  # skip (non-positive)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = tick_anom.audit_tick_file(path)
    assert summary["n_total"] == 4
    assert summary["n_ok"] == 2
    assert summary["n_warn"] == 1
    assert summary["n_skip"] == 1


# ────────────────────────────────────────────────────────────────────
# l2_cpcv
# ────────────────────────────────────────────────────────────────────


def test_cpcv_too_few_samples_returns_empty() -> None:
    report = l2_cpcv.cpcv([1.0] * 10)
    assert report.n_splits == 0


def test_cpcv_generates_expected_split_count() -> None:
    rng = random.Random(42)
    returns = [rng.gauss(0.1, 1.0) for _ in range(60)]
    report = l2_cpcv.cpcv(returns, n_folds=6, k_test=2)
    # C(6, 2) = 15 splits
    assert report.n_splits == 15


def test_cpcv_train_excludes_test_and_purge() -> None:
    folds = l2_cpcv._build_fold_indices(60, 6)
    # test fold = first one (0..10)
    train_idx = l2_cpcv._purged_train_indices(
        60, [folds[0]], purge_size=5, embargo_size=5)
    # Test indices 0..10 excluded; embargo 10..15 also excluded
    # First training index should be 15
    assert min(train_idx) == 15
    assert 5 not in train_idx
    assert 15 in train_idx


def test_cpcv_score_stability_measure() -> None:
    """A series of equal returns → sample sharpe = 0 (no variance)."""
    report = l2_cpcv.cpcv([1.0] * 60, n_folds=4, k_test=2)
    # All scores will be 0 (constant series) → stddev = 0
    # sample_sharpe = mean / stddev = 0/0 → defined as 0
    assert report.sample_sharpe == 0.0


def test_cpcv_with_positive_mean_returns_positive_score() -> None:
    """All +0.5 returns → every fold has positive mean."""
    report = l2_cpcv.cpcv([0.5] * 60, n_folds=4, k_test=2, metric="mean")
    assert report.test_score_mean == 0.5


# ────────────────────────────────────────────────────────────────────
# l2_performance_attribution
# ────────────────────────────────────────────────────────────────────


def test_attribution_perfect_fill_alpha_only() -> None:
    """Trade with no slip, no timing → entire pnl is alpha."""
    signal = {
        "signal_id": "s1", "side": "LONG",
        "entry_price": 100.0,
        "intended_stop_price": 99.0,
        "intended_target_price": 102.0,
    }
    entry_fill = {"actual_fill_price": 100.0, "exit_reason": "ENTRY"}
    exit_fill = {"actual_fill_price": 102.0, "exit_reason": "TARGET"}
    attr = pa.attribute_trade(
        signal=signal, entry_fill=entry_fill, exit_fill=exit_fill,
        point_value=2.0, commission_per_rt=0.5,
    )
    assert attr.entry_timing == 0.0  # exact fill
    assert attr.exit_slip == 0.0     # hit target exactly
    # Total pnl = (102-100)*2 - 0.5 = 3.5
    assert attr.pnl_total == 3.5


def test_attribution_decomposes_entry_timing() -> None:
    """Trade filled 0.5 below intended → +1 USD entry_timing."""
    signal = {
        "signal_id": "s1", "side": "LONG",
        "entry_price": 100.0,
        "intended_stop_price": 99.0,
        "intended_target_price": 102.0,
    }
    entry_fill = {"actual_fill_price": 99.5, "exit_reason": "ENTRY"}
    exit_fill = {"actual_fill_price": 102.0, "exit_reason": "TARGET"}
    attr = pa.attribute_trade(
        signal=signal, entry_fill=entry_fill, exit_fill=exit_fill,
        point_value=2.0, commission_per_rt=0.5,
    )
    assert attr.entry_timing == 1.0  # (100 - 99.5) * 2 = +1 USD


def test_attribution_no_data_returns_empty(tmp_path: Path,
                                              monkeypatch) -> None:
    monkeypatch.setattr(pa, "SIGNAL_LOG", tmp_path / "sig.jsonl")
    monkeypatch.setattr(pa, "BROKER_FILL_LOG", tmp_path / "fill.jsonl")
    monkeypatch.setattr(pa, "ATTRIBUTION_LOG", tmp_path / "attr.jsonl")
    report = pa.run_attribution()
    assert report.n_trades == 0


# ────────────────────────────────────────────────────────────────────
# l2_slippage_predictor
# ────────────────────────────────────────────────────────────────────


def test_slip_predict_with_no_model_returns_default() -> None:
    assert slip.predict_slip(model=None) == 1.0


def test_slip_predict_uses_bucket_data() -> None:
    model = slip.SlipModel(
        ts_trained=datetime.now(UTC).isoformat(),
        n_fills=10, default_slip_ticks=1.0,
        buckets=[
            slip.SlipBucket(
                regime="NORMAL", session="RTH_MID",
                size_bucket="1", vol_bucket="mid",
                n=10, mean_slip_ticks=0.5,
                p90_slip_ticks=1.5, stddev_slip_ticks=0.3),
        ],
    )
    pred = slip.predict_slip(
        regime="NORMAL", session="RTH_MID", size=1, vol=None,
        model=model)
    assert pred == 0.5


def test_slip_predict_falls_back_to_session_match() -> None:
    """When exact bucket doesn't match, fall back to session-only."""
    model = slip.SlipModel(
        ts_trained=datetime.now(UTC).isoformat(),
        n_fills=20, default_slip_ticks=1.0,
        buckets=[
            slip.SlipBucket(
                regime="WIDE", session="RTH_OPEN",
                size_bucket="2-5", vol_bucket="high",
                n=10, mean_slip_ticks=3.0,
                p90_slip_ticks=5.0, stddev_slip_ticks=1.0),
            slip.SlipBucket(
                regime="NORMAL", session="RTH_OPEN",
                size_bucket="1", vol_bucket="low",
                n=10, mean_slip_ticks=1.5,
                p90_slip_ticks=2.5, stddev_slip_ticks=0.5),
        ],
    )
    # Different regime/size/vol but same session → fallback to mean of
    # all RTH_OPEN buckets = (3.0 + 1.5) / 2 = 2.25
    pred = slip.predict_slip(
        regime="OTHER", session="RTH_OPEN", size=99, vol=None,
        model=model)
    assert pred == 2.25


def test_slip_train_with_no_fills(tmp_path: Path) -> None:
    """train_model with empty inputs returns 0-bucket model."""
    model = slip.train_model(
        _fill_path=tmp_path / "fill.jsonl",
        _signal_path=tmp_path / "sig.jsonl",
    )
    assert model.n_fills == 0
    assert len(model.buckets) == 0


def test_slip_size_bucket_classification() -> None:
    assert slip._size_bucket(1) == "1"
    assert slip._size_bucket(3) == "2-5"
    assert slip._size_bucket(8) == "6-10"
    assert slip._size_bucket(50) == "10+"


# ────────────────────────────────────────────────────────────────────
# l2_strategy_ensemble
# ────────────────────────────────────────────────────────────────────


@dataclass
class _StubSig:
    side: str
    confidence: float
    strategy_id: str
    signal_id: str = "stub"


def test_ensemble_no_signals_returns_none() -> None:
    out = ens.vote([], {"book_imbalance": 1.0})
    assert out is None


def test_ensemble_unanimous_long_fires() -> None:
    weights = {"book_imbalance": 1.0, "microprice_drift": 1.0,
                "footprint_absorption": 1.0}
    signals = [
        _StubSig(side="LONG", confidence=0.8, strategy_id="book_imbalance"),
        _StubSig(side="LONG", confidence=0.7, strategy_id="microprice_drift"),
        _StubSig(side="LONG", confidence=0.6, strategy_id="footprint_absorption"),
    ]
    out = ens.vote(signals, weights, ensemble_threshold=0.5)
    assert out is not None
    assert out.side == "LONG"
    assert out.weighted_vote > 0.5


def test_ensemble_conflicting_signals_below_threshold_returns_none() -> None:
    weights = {"a": 1.0, "b": 1.0}
    signals = [
        _StubSig(side="LONG", confidence=0.5, strategy_id="a"),
        _StubSig(side="SHORT", confidence=0.5, strategy_id="b"),
    ]
    # Net vote = 0 → below any positive threshold
    out = ens.vote(signals, weights, ensemble_threshold=0.5)
    assert out is None


def test_ensemble_ignores_zero_weight_strategies() -> None:
    """A strategy with weight=0 (no history) doesn't contribute."""
    weights = {"good": 1.0, "untracked": 0.0}
    signals = [
        _StubSig(side="LONG", confidence=0.8, strategy_id="good"),
        _StubSig(side="SHORT", confidence=0.8, strategy_id="untracked"),
    ]
    out = ens.vote(signals, weights, ensemble_threshold=0.5)
    assert out is not None
    assert out.side == "LONG"
    # untracked should not appear in constituents
    assert all(c["strategy_id"] != "untracked" for c in out.constituent_signals)


def test_ensemble_compute_weights_from_history(tmp_path: Path) -> None:
    path = tmp_path / "bt.jsonl"
    now = datetime.now(UTC)
    records = [
        {"ts": now.isoformat(), "strategy": "book_imbalance",
          "sharpe_proxy": 0.8, "sharpe_proxy_valid": True},
        {"ts": now.isoformat(), "strategy": "microprice_drift",
          "sharpe_proxy": -0.5, "sharpe_proxy_valid": True},  # negative
        {"ts": now.isoformat(), "strategy": "aggressor_flow",
          "sharpe_proxy": 1.2, "sharpe_proxy_valid": True},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                     encoding="utf-8")
    weights = ens.compute_weights_from_history(_path=path)
    # Negative sharpe should be floored to 0
    assert weights.weights["book_imbalance"] == 0.8
    assert weights.weights["microprice_drift"] == 0.0
    assert weights.weights["aggressor_flow"] == 1.2


# ────────────────────────────────────────────────────────────────────
# l2_cross_broker_arb
# ────────────────────────────────────────────────────────────────────


def test_arb_no_data_returns_empty(tmp_path: Path) -> None:
    report = arb.check_discrepancy([], [])
    assert report.n_pairs_checked == 0


def test_arb_no_discrepancy_when_prices_match() -> None:
    snaps_a = [{"epoch_s": 1.0, "mid": 100.0, "spread": 0.25}] * 5
    snaps_b = [{"epoch_s": 1.0, "mid": 100.0, "spread": 0.25}] * 5
    report = arb.check_discrepancy(snaps_a, snaps_b, threshold_bps=5.0)
    assert report.n_discrepancies == 0


def test_arb_flags_discrepancy_above_threshold() -> None:
    # 100 bps = 1% difference
    snaps_a = [{"epoch_s": float(i), "mid": 100.0, "spread": 0.25}
                for i in range(10)]
    snaps_b = [{"epoch_s": float(i), "mid": 101.0, "spread": 0.25}
                for i in range(10)]
    report = arb.check_discrepancy(snaps_a, snaps_b, threshold_bps=50.0)
    assert report.n_pairs_checked == 10
    assert report.n_discrepancies == 10
    assert report.max_diff_bps is not None
    assert report.max_diff_bps > 50.0


def test_arb_pair_by_time_aligns_correctly() -> None:
    snaps_a = [{"epoch_s": float(i), "mid": 100.0, "spread": 0.25}
                for i in range(0, 100, 10)]
    snaps_b = [{"epoch_s": float(i + 1), "mid": 100.0, "spread": 0.25}
                for i in range(0, 100, 10)]
    pairs = arb._pair_by_time(snaps_a, snaps_b, max_skew_seconds=2.0)
    assert len(pairs) == 10  # all within 1s skew


def test_arb_skew_filter_drops_far_pairs() -> None:
    """Snaps >5s apart should not pair when max_skew_seconds=2."""
    snaps_a = [{"epoch_s": 0.0, "mid": 100.0, "spread": 0.25}]
    snaps_b = [{"epoch_s": 10.0, "mid": 100.0, "spread": 0.25}]
    pairs = arb._pair_by_time(snaps_a, snaps_b, max_skew_seconds=2.0)
    assert len(pairs) == 0
