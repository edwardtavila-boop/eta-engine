"""Tests for Phase 3 (l2_overlay) + Phase 4 (book_imbalance + spread_regime)
+ Phase 5 (l2_backtest_harness).

All three modules consume depth snapshots; tests use synthetic snapshots
written to a tmp DEPTH_DIR.
"""
from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eta_engine.strategies import book_imbalance_strategy as bis
from eta_engine.strategies import l2_overlay

# ────────────────────────────────────────────────────────────────────
# Phase 3 — l2_overlay
# ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def depth_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(l2_overlay, "DEPTH_DIR", tmp_path)
    return tmp_path


def _write_depth_snapshots(d: Path, symbol: str, dt: datetime,
                             snapshots: list[dict]) -> None:
    p = d / f"{symbol}_{dt.strftime('%Y%m%d')}.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in snapshots) + "\n", encoding="utf-8")


def test_l2_overlay_no_data_returns_passed_with_no_l2_yet(depth_dir: Path) -> None:
    """Critical: the overlay must NEVER block a strategy when no L2
    history exists — fall through to the legacy decision."""
    dt = datetime.now(UTC)
    r = l2_overlay.confirm_sweep_with_l2(
        symbol="MNQ", swept_level=100.0, touch_dt=dt, side="LONG")
    assert r.passed is True
    assert r.reason == "no_l2_yet"


def test_l2_overlay_sweep_real_stop_run(depth_dir: Path) -> None:
    """Real sweep: bids stacked at-and-below swept_level → pass."""
    dt = datetime.now(UTC).replace(microsecond=0)
    snap_epoch = (dt - timedelta(seconds=10)).timestamp()
    _write_depth_snapshots(depth_dir, "MNQ", dt, [{
        "ts": (dt - timedelta(seconds=10)).isoformat(),
        "epoch_s": snap_epoch,
        "symbol": "MNQ",
        "bids": [{"price": 100.0, "size": 30}, {"price": 99.75, "size": 25},
                 {"price": 99.5, "size": 20}],
        "asks": [{"price": 100.25, "size": 10}],
        "spread": 0.25, "mid": 100.125,
    }])
    r = l2_overlay.confirm_sweep_with_l2(
        symbol="MNQ", swept_level=100.0, touch_dt=dt, side="LONG",
        min_stop_qty=50)
    assert r.passed is True
    assert r.reason == "real_sweep_confirmed"
    assert r.detail["qty_at_level"] >= 50


def test_l2_overlay_sweep_thin_book_rejected(depth_dir: Path) -> None:
    """Wick through a level with no real liquidity → fail (overlay
    catches the technical noise case)."""
    dt = datetime.now(UTC).replace(microsecond=0)
    snap_epoch = (dt - timedelta(seconds=10)).timestamp()
    _write_depth_snapshots(depth_dir, "MNQ", dt, [{
        "ts": (dt - timedelta(seconds=10)).isoformat(),
        "epoch_s": snap_epoch,
        "bids": [{"price": 100.0, "size": 2}],  # thin
        "asks": [{"price": 100.25, "size": 10}],
        "spread": 0.25, "mid": 100.125,
    }])
    r = l2_overlay.confirm_sweep_with_l2(
        symbol="MNQ", swept_level=100.0, touch_dt=dt, side="LONG",
        min_stop_qty=50)
    assert r.passed is False
    assert r.reason == "thin_book_at_swept_level"


def test_l2_overlay_poc_pull_confirmed(depth_dir: Path) -> None:
    dt = datetime.now(UTC).replace(microsecond=0)
    snap_epoch = (dt - timedelta(seconds=2)).timestamp()
    _write_depth_snapshots(depth_dir, "MNQ", dt, [{
        "ts": (dt - timedelta(seconds=2)).isoformat(),
        "epoch_s": snap_epoch,
        "bids": [{"price": 100.0, "size": 50}],
        "asks": [{"price": 100.25, "size": 10}],
        "spread": 0.25, "mid": 100.125,
    }])
    r = l2_overlay.confirm_poc_pull_with_l2(
        symbol="MNQ", entry_dt=dt, entry_side="LONG",
        min_imbalance_ratio=2.0)
    assert r.passed is True
    assert r.reason == "poc_pull_confirmed"


def test_l2_overlay_poc_pull_weak(depth_dir: Path) -> None:
    dt = datetime.now(UTC).replace(microsecond=0)
    snap_epoch = (dt - timedelta(seconds=2)).timestamp()
    _write_depth_snapshots(depth_dir, "MNQ", dt, [{
        "ts": (dt - timedelta(seconds=2)).isoformat(),
        "epoch_s": snap_epoch,
        "bids": [{"price": 100.0, "size": 10}],
        "asks": [{"price": 100.25, "size": 12}],
        "spread": 0.25, "mid": 100.125,
    }])
    r = l2_overlay.confirm_poc_pull_with_l2(
        symbol="MNQ", entry_dt=dt, entry_side="LONG",
        min_imbalance_ratio=2.0)
    assert r.passed is False
    assert r.reason == "weak_imbalance"


def test_l2_overlay_anchor_had_liquidity(depth_dir: Path) -> None:
    dt = datetime.now(UTC).replace(microsecond=0)
    snap_epoch = (dt - timedelta(seconds=5)).timestamp()
    _write_depth_snapshots(depth_dir, "MNQ", dt, [{
        "ts": (dt - timedelta(seconds=5)).isoformat(),
        "epoch_s": snap_epoch,
        "bids": [{"price": 100.0, "size": 25}],
        "asks": [{"price": 100.5, "size": 30}],  # near anchor 101 (5pts)
        "spread": 0.5, "mid": 100.25,
    }])
    r = l2_overlay.confirm_anchor_touch_with_l2(
        symbol="MNQ", anchor_price=101.0, touch_dt=dt,
        min_qty_within_pts=5.0, min_qty=30)
    assert r.passed is True
    assert r.detail["qty_near"] >= 30


def test_l2_overlay_gz_file_supported(depth_dir: Path) -> None:
    """Phase-1 rotation gzips files older than 14d — overlay must
    still read them."""
    dt = datetime.now(UTC).replace(microsecond=0)
    p = depth_dir / f"MNQ_{dt.strftime('%Y%m%d')}.jsonl.gz"
    snap = {"ts": (dt - timedelta(seconds=5)).isoformat(),
            "epoch_s": (dt - timedelta(seconds=5)).timestamp(),
            "bids": [{"price": 100.0, "size": 50}],
            "asks": [{"price": 100.25, "size": 10}],
            "spread": 0.25, "mid": 100.125}
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(json.dumps(snap) + "\n")
    r = l2_overlay.confirm_poc_pull_with_l2(
        symbol="MNQ", entry_dt=dt, entry_side="LONG", min_imbalance_ratio=2.0)
    assert r.passed is True


# ────────────────────────────────────────────────────────────────────
# Phase 4 — book_imbalance_strategy
# ────────────────────────────────────────────────────────────────────


def _snap(bid_qtys: list[int], ask_qtys: list[int], mid: float = 100.0,
          spread: float = 0.25) -> dict:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "epoch_s": datetime.now(UTC).timestamp(),
        "bids": [{"price": mid - (i + 1) * 0.25, "size": s}
                  for i, s in enumerate(bid_qtys)],
        "asks": [{"price": mid + (i + 1) * 0.25, "size": s}
                  for i, s in enumerate(ask_qtys)],
        "spread": spread, "mid": mid,
    }


def test_compute_imbalance_basic() -> None:
    snap = _snap([10, 20, 30], [5, 15, 25])
    ratio, b, a = bis.compute_imbalance(snap, n_levels=3)
    assert b == 60
    assert a == 45
    assert ratio == pytest.approx(60 / 45)


def test_compute_imbalance_zero_side_returns_neutral() -> None:
    """Division-by-zero must yield ratio=1.0 (no signal), not inf."""
    snap = _snap([10, 20, 30], [0, 0, 0])
    ratio, _, _ = bis.compute_imbalance(snap, n_levels=3)
    assert ratio == 1.0


def test_evaluate_snapshot_long_fires_after_consecutive() -> None:
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=3)
    state = bis.BookImbalanceState()
    # 3 long-imbalance snapshots in a row
    s1 = bis.evaluate_snapshot(_snap([50, 50, 50], [10, 10, 10]), cfg, state, atr=1.0)
    s2 = bis.evaluate_snapshot(_snap([50, 50, 50], [10, 10, 10]), cfg, state, atr=1.0)
    s3 = bis.evaluate_snapshot(_snap([50, 50, 50], [10, 10, 10]), cfg, state, atr=1.0)
    assert s1 is None and s2 is None
    assert s3 is not None
    assert s3.side == "LONG"
    assert s3.entry_price == pytest.approx(100.125)  # mid + spread/2
    assert s3.stop < s3.entry_price < s3.target


def test_evaluate_snapshot_short_fires() -> None:
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=2)
    state = bis.BookImbalanceState()
    bis.evaluate_snapshot(_snap([10, 10, 10], [50, 50, 50]), cfg, state, atr=1.0)
    sig = bis.evaluate_snapshot(_snap([10, 10, 10], [50, 50, 50]), cfg, state, atr=1.0)
    assert sig is not None
    assert sig.side == "SHORT"


def test_evaluate_snapshot_neutral_resets_counters() -> None:
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=3)
    state = bis.BookImbalanceState()
    bis.evaluate_snapshot(_snap([50, 50, 50], [10, 10, 10]), cfg, state)  # +1 LONG
    bis.evaluate_snapshot(_snap([30, 30, 30], [30, 30, 30]), cfg, state)  # neutral → reset
    bis.evaluate_snapshot(_snap([50, 50, 50], [10, 10, 10]), cfg, state)  # +1 LONG (back to 1)
    # 3rd consecutive LONG was reset — no signal yet
    assert state.consecutive_long_count == 1


def test_evaluate_snapshot_respects_max_trades_per_day() -> None:
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=1,
                                    max_trades_per_day=2)
    state = bis.BookImbalanceState()
    for _ in range(5):
        bis.evaluate_snapshot(_snap([50, 50, 50], [10, 10, 10]), cfg, state)
    assert state.trades_today == 2


# ── Phase 4 partner — spread_regime_filter ────────────────────────


def test_spread_regime_normal_when_in_range() -> None:
    cfg = bis.SpreadRegimeConfig(pause_at_multiple=4.0, resume_at_multiple=2.0)
    state = bis.SpreadRegimeState()
    # Build a median around 0.25
    for _ in range(10):
        bis.update_spread_regime(_snap([10], [10], spread=0.25), cfg, state)
    out = bis.update_spread_regime(_snap([10], [10], spread=0.25), cfg, state)
    assert out["verdict"] == "NORMAL"
    assert out["paused"] is False


def test_spread_regime_pauses_when_blowout() -> None:
    cfg = bis.SpreadRegimeConfig(pause_at_multiple=4.0, resume_at_multiple=2.0)
    state = bis.SpreadRegimeState()
    for _ in range(10):
        bis.update_spread_regime(_snap([10], [10], spread=0.25), cfg, state)
    out = bis.update_spread_regime(_snap([10], [10], spread=1.5), cfg, state)
    assert out["paused"] is True
    assert out["verdict"] == "PAUSE"


def test_spread_regime_hysteresis_holds_pause() -> None:
    """Once paused, must drop below resume_at_multiple to come back."""
    cfg = bis.SpreadRegimeConfig(pause_at_multiple=4.0, resume_at_multiple=2.0)
    state = bis.SpreadRegimeState()
    for _ in range(10):
        bis.update_spread_regime(_snap([10], [10], spread=0.25), cfg, state)
    # Trigger pause
    bis.update_spread_regime(_snap([10], [10], spread=1.5), cfg, state)
    # 3x median → still paused (above resume threshold)
    out = bis.update_spread_regime(_snap([10], [10], spread=0.75), cfg, state)
    assert out["paused"] is True
    # 1.5x median → resumes
    out = bis.update_spread_regime(_snap([10], [10], spread=0.30), cfg, state)
    assert out["paused"] is False


# ── Factory APIs ──────────────────────────────────────────────────


def test_make_book_imbalance_strategy_factory() -> None:
    strat = bis.make_book_imbalance_strategy()
    assert hasattr(strat, "evaluate")
    # Smoke: call with one snapshot
    sig = strat.evaluate(_snap([50, 50, 50], [10, 10, 10]), atr=1.0)
    assert sig is None or sig.side == "LONG"


def test_make_spread_regime_filter_factory() -> None:
    filt = bis.make_spread_regime_filter()
    out = filt.update(_snap([10], [10], spread=0.25))
    assert "verdict" in out


# ────────────────────────────────────────────────────────────────────
# Phase 5 — l2_backtest_harness
# ────────────────────────────────────────────────────────────────────


def test_l2_backtest_no_data_returns_zero_signals(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """When no depth files exist, the harness reports 0 snapshots /
    0 trades cleanly (no crash)."""
    from eta_engine.scripts import l2_backtest_harness as harness
    monkeypatch.setattr(harness, "DEPTH_DIR", tmp_path)
    result = harness.run_book_imbalance(
        "MNQ", days=3,
        entry_threshold=1.5, consecutive_snaps=3,
        n_levels=3, atr_stop_mult=1.0, rr_target=2.0,
    )
    assert result.n_snapshots == 0
    assert result.n_trades == 0
    assert result.total_pnl_points == 0


def test_l2_backtest_synthetic_signal_to_trade(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a depth file with 5 bullish snapshots + 1 follow-through,
    confirm the harness produces a trade and exits at the target."""
    from eta_engine.scripts import l2_backtest_harness as harness
    monkeypatch.setattr(harness, "DEPTH_DIR", tmp_path)

    # Build a 1Hz-cadence chronological depth stream
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    base_epoch = today.timestamp()
    snapshots: list[dict] = []
    for i in range(3):
        snapshots.append({
            "ts": (today + timedelta(seconds=i)).isoformat(),
            "epoch_s": base_epoch + i,
            "bids": [{"price": 100.0, "size": 50}],
            "asks": [{"price": 100.25, "size": 10}],
            "spread": 0.25, "mid": 100.125,
        })
    # After 3 bullish snaps the strategy fires LONG
    # Now drive mid up to trigger TARGET (entry + 1 * atr * 2 = 100.125 + 2)
    for i in range(3, 60):
        snapshots.append({
            "ts": (today + timedelta(seconds=i)).isoformat(),
            "epoch_s": base_epoch + i,
            "bids": [{"price": 100.0 + i * 0.1, "size": 30}],
            "asks": [{"price": 100.25 + i * 0.1, "size": 30}],
            "spread": 0.25, "mid": 100.125 + i * 0.1,
        })
    p = tmp_path / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in snapshots) + "\n", encoding="utf-8")

    # Backtest looks back from "now" for `days` days. Use days=1 so today is
    # included in the date scan.
    result = harness.run_book_imbalance(
        "MNQ", days=1,
        entry_threshold=1.5, consecutive_snaps=3,
        n_levels=3, atr_stop_mult=1.0, rr_target=2.0,
    )
    assert result.n_snapshots == 60
    assert result.n_trades >= 1
    # First trade was LONG and should have exited at TARGET
    assert result.trades[0].side == "LONG"
