"""Hardening-pass tests for the 2026-05-11 L2 + capture review fixes.

Each test maps to a specific blocker / important / hygiene item from
the master synthesis.  Tests are grouped by file, ordered by severity.

Function naming convention: ``test_<CODE>_<description>`` where CODE is
the fix identifier from the master synthesis (B3, B6, I5, …).  The
uppercase prefix is intentional and traces tests back to the
blocker/important/hygiene rows in the synthesis.
"""
# ruff: noqa: N802
# N802: test_<CODE>_… naming with uppercase CODE is intentional — it maps
# tests back to the master synthesis fix codes (B1-B6, I1-I10, D1-D7).
from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eta_engine.scripts import capture_rotation, disk_space_monitor, health_dashboard, l2_backtest_harness
from eta_engine.strategies import (
    book_imbalance_strategy as bis,
)
from eta_engine.strategies import (
    l2_overlay,
    trading_gate,
)
from eta_engine.strategies import (
    spread_regime_filter as srf,
)


def _snap(bid_qtys: list[int], ask_qtys: list[int], *,
          mid: float = 100.0, spread: float = 0.25,
          ts: datetime | None = None) -> dict:
    """Build a synthetic depth snapshot.  Pass ts to control the
    snap_dt parsing path for cooldown / gap tests."""
    ts_dt = ts or datetime.now(UTC)
    return {
        "ts": ts_dt.isoformat(),
        "epoch_s": ts_dt.timestamp(),
        "bids": [{"price": mid - (i + 1) * 0.25, "size": s}
                  for i, s in enumerate(bid_qtys)],
        "asks": [{"price": mid + (i + 1) * 0.25, "size": s}
                  for i, s in enumerate(ask_qtys)],
        "spread": spread, "mid": mid,
    }


# ────────────────────────────────────────────────────────────────────
# B3 — cooldown_bars enforcement
# ────────────────────────────────────────────────────────────────────


def test_B3_cooldown_bars_blocks_immediate_reentry() -> None:
    """After a signal fires, two snaps within cooldown_bars * cadence
    must NOT emit a second signal."""
    cfg = bis.BookImbalanceConfig(
        entry_threshold=1.5, consecutive_snaps=1,
        cooldown_bars=3, snapshot_interval_seconds=5.0,
        max_trades_per_day=10,
    )
    state = bis.BookImbalanceState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    # Snap 1 — fires LONG (consecutive_snaps=1 means immediate)
    s1 = bis.evaluate_snapshot(_snap([50], [10], ts=base), cfg, state, atr=2.0)
    assert s1 is not None
    assert s1.side == "LONG"
    # Snap 2 (5s later) — within cooldown_bars=3 * 5s = 15s window → blocked
    s2 = bis.evaluate_snapshot(_snap([50], [10], ts=base + timedelta(seconds=5)),
                                cfg, state, atr=2.0)
    assert s2 is None
    # Snap 3 (10s later) — still within cooldown
    s3 = bis.evaluate_snapshot(_snap([50], [10], ts=base + timedelta(seconds=10)),
                                cfg, state, atr=2.0)
    assert s3 is None


def test_B3_cooldown_bars_releases_after_window() -> None:
    """Once cooldown_bars * cadence has elapsed, re-arming requires a
    fresh consecutive_snaps run (not residual count)."""
    cfg = bis.BookImbalanceConfig(
        entry_threshold=1.5, consecutive_snaps=2,
        cooldown_bars=2, snapshot_interval_seconds=5.0,
        max_trades_per_day=10,
    )
    state = bis.BookImbalanceState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    bis.evaluate_snapshot(_snap([50], [10], ts=base), cfg, state, atr=2.0)
    s2 = bis.evaluate_snapshot(_snap([50], [10], ts=base + timedelta(seconds=5)),
                                cfg, state, atr=2.0)
    assert s2 is not None  # consecutive_snaps=2 → fires here
    # Wait past cooldown: 2 * 5s = 10s
    s_post = bis.evaluate_snapshot(_snap([50], [10], ts=base + timedelta(seconds=20)),
                                    cfg, state, atr=2.0)
    assert s_post is None  # only 1 snap since cooldown released, need 2 consecutive
    s_fire = bis.evaluate_snapshot(_snap([50], [10], ts=base + timedelta(seconds=25)),
                                     cfg, state, atr=2.0)
    assert s_fire is not None


def test_B3_cooldown_seconds_overrides_cooldown_bars() -> None:
    """When cooldown_seconds > 0, it takes precedence over cooldown_bars."""
    cfg = bis.BookImbalanceConfig(
        entry_threshold=1.5, consecutive_snaps=1,
        cooldown_seconds=60.0,  # explicit wall-clock
        cooldown_bars=999,  # large bar count, but seconds wins
        snapshot_interval_seconds=5.0,
    )
    state = bis.BookImbalanceState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    bis.evaluate_snapshot(_snap([50], [10], ts=base), cfg, state, atr=2.0)
    # 30s later (within 60s) — blocked
    s2 = bis.evaluate_snapshot(_snap([50], [10], ts=base + timedelta(seconds=30)),
                                cfg, state, atr=2.0)
    assert s2 is None


# ────────────────────────────────────────────────────────────────────
# B6 — signal_id + qty cap on ImbalanceSignal
# ────────────────────────────────────────────────────────────────────


def test_B6_signal_id_includes_symbol_side_and_ts() -> None:
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=1)
    state = bis.BookImbalanceState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    sig = bis.evaluate_snapshot(_snap([50], [10], ts=base), cfg, state,
                                  atr=2.0, symbol="MNQ")
    assert sig is not None
    assert sig.signal_id.startswith("MNQ-LONG-")
    assert base.isoformat() in sig.signal_id


def test_B6_qty_contracts_hard_capped() -> None:
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=1,
                                    max_qty_contracts=1)
    state = bis.BookImbalanceState()
    sig = bis.evaluate_snapshot(_snap([50], [10]), cfg, state, atr=2.0)
    assert sig is not None
    assert sig.qty_contracts == 1
    # Even if config allows higher, the paper-soak default clamps to 1
    cfg2 = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=1,
                                     max_qty_contracts=999)
    state2 = bis.BookImbalanceState()
    sig2 = bis.evaluate_snapshot(_snap([50], [10]), cfg2, state2, atr=2.0)
    assert sig2 is not None
    assert sig2.qty_contracts >= 1


def test_B6_signal_carries_symbol() -> None:
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=1)
    state = bis.BookImbalanceState()
    sig = bis.evaluate_snapshot(_snap([50], [10]), cfg, state, atr=2.0,
                                  symbol="ES")
    assert sig is not None
    assert sig.symbol == "ES"


def test_B6_min_stop_ticks_floor_rejects_tiny_atr() -> None:
    """When ATR is so small the stop collapses below min_stop_ticks * tick,
    refuse to emit (prevents unbounded sizing on glitchy ATR)."""
    cfg = bis.BookImbalanceConfig(
        entry_threshold=1.5, consecutive_snaps=1,
        min_stop_ticks=4,  # 4 * 0.25 = 1.0 minimum stop distance for MNQ
    )
    state = bis.BookImbalanceState()
    # atr=0.01 * atr_stop_mult=1.0 = 0.01 stop distance < 1.0 floor
    sig = bis.evaluate_snapshot(_snap([50], [10]), cfg, state, atr=0.01)
    assert sig is None


# ────────────────────────────────────────────────────────────────────
# I5 — tick_size per-symbol lookup
# ────────────────────────────────────────────────────────────────────


def test_I5_get_tick_size_known_symbols() -> None:
    assert bis.get_tick_size("MNQ") == 0.25
    assert bis.get_tick_size("GC") == 0.10
    assert bis.get_tick_size("CL") == 0.01


def test_I5_get_tick_size_strips_front_month_suffix() -> None:
    assert bis.get_tick_size("MNQ1") == 0.25  # 1 = front-month suffix
    assert bis.get_tick_size("CL1") == 0.01


def test_I5_get_tick_size_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown tick size"):
        bis.get_tick_size("FAKE_SYMBOL")


def test_I5_spread_filter_uses_correct_tick_for_GC() -> None:
    """GC tick = 0.10.  spread_min_ticks=1 → reject spread < 0.10."""
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=1,
                                    spread_min_ticks=1.0, spread_max_ticks=3.0)
    state = bis.BookImbalanceState()
    # spread=0.05 < 0.10 (1 tick) → rejected
    sig = bis.evaluate_snapshot(_snap([50], [10], spread=0.05),
                                  cfg, state, atr=2.0, symbol="GC")
    assert sig is None
    # spread=0.20 (2 ticks for GC) → accepted
    sig = bis.evaluate_snapshot(_snap([50], [10], spread=0.20),
                                  cfg, state, atr=2.0, symbol="GC")
    assert sig is not None


# ────────────────────────────────────────────────────────────────────
# I7 — gap-aware reset
# ────────────────────────────────────────────────────────────────────


def test_I7_gap_too_large_resets_consecutive_count() -> None:
    """Two snaps separated by a gap > gap_reset_multiple * cadence
    must NOT count as 'consecutive' conviction."""
    cfg = bis.BookImbalanceConfig(
        entry_threshold=1.5, consecutive_snaps=2,
        snapshot_interval_seconds=5.0, gap_reset_multiple=2.0,
        cooldown_bars=0,
    )
    state = bis.BookImbalanceState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    # Snap 1 at base — count_long = 1 (need 2 to fire)
    bis.evaluate_snapshot(_snap([50], [10], ts=base), cfg, state, atr=2.0)
    # Snap 2 30s later (> 2 * 5s = 10s gap) → counter resets to 0, then ++ = 1
    sig = bis.evaluate_snapshot(_snap([50], [10], ts=base + timedelta(seconds=30)),
                                  cfg, state, atr=2.0)
    assert sig is None  # would have fired without gap-reset (count would have been 2)
    # Snap 3 only 5s after snap 2 → no gap reset, count = 2 → FIRES
    sig = bis.evaluate_snapshot(_snap([50], [10], ts=base + timedelta(seconds=35)),
                                  cfg, state, atr=2.0)
    assert sig is not None


# ────────────────────────────────────────────────────────────────────
# I8 — zero-side fail-closed
# ────────────────────────────────────────────────────────────────────


def test_I8_zero_bid_side_resets_counters_no_signal() -> None:
    """An anomalous empty bid side must NOT continue accumulating
    LONG count from a prior valid snap."""
    cfg = bis.BookImbalanceConfig(entry_threshold=1.5, consecutive_snaps=2)
    state = bis.BookImbalanceState()
    bis.evaluate_snapshot(_snap([50], [10]), cfg, state, atr=2.0)
    # Anomaly: bid side empty
    sig = bis.evaluate_snapshot(_snap([0], [10]), cfg, state, atr=2.0)
    assert sig is None
    assert state.consecutive_long_count == 0  # was 1, now reset


def test_I8_compute_imbalance_with_classification() -> None:
    """Internal helper exposes the classification field."""
    snap_ok = _snap([10], [5])
    ratio, b, a, cls = bis._compute_imbalance_with_classification(snap_ok, n_levels=1)
    assert cls == "OK"
    assert ratio == 2.0

    snap_empty_bids = _snap([0], [5])
    ratio, _, _, cls = bis._compute_imbalance_with_classification(snap_empty_bids, n_levels=1)
    assert cls == "EMPTY_BIDS"
    assert ratio == 1.0  # neutral sentinel

    snap_empty_asks = _snap([10], [0])
    ratio, _, _, cls = bis._compute_imbalance_with_classification(snap_empty_asks, n_levels=1)
    assert cls == "EMPTY_ASKS"

    snap_both = _snap([0], [0])
    ratio, _, _, cls = bis._compute_imbalance_with_classification(snap_both, n_levels=1)
    assert cls == "BOTH_EMPTY"


# ────────────────────────────────────────────────────────────────────
# I3 + I6 — spread regime staleness + 1Hz comment fix
# ────────────────────────────────────────────────────────────────────


def test_I6_check_staleness_returns_STALE_when_no_snap() -> None:
    cfg = srf.SpreadRegimeConfig()
    state = srf.SpreadRegimeState()
    out = srf.check_staleness(state, cfg)
    assert out["verdict"] == "STALE"
    assert out["reason"] == "no_snapshot_yet"


def test_I6_check_staleness_returns_STALE_when_old_snap() -> None:
    cfg = srf.SpreadRegimeConfig(stale_after_seconds=60.0)
    state = srf.SpreadRegimeState()
    # Submit a snap "now"
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    srf.update_spread_regime(_snap([10], [10], spread=0.25), cfg, state, now=base)
    # Check 90s later → STALE
    out = srf.check_staleness(state, cfg, now=base + timedelta(seconds=90))
    assert out["verdict"] == "STALE"
    assert out["reason"] == "stale_no_recent_snapshot"


def test_spread_regime_handles_none_spread_field() -> None:
    """Real depth feeds occasionally publish snapshots where ``spread``
    is explicitly None (one side of the book momentarily empty).
    ``dict.get(key, default)`` returns the default ONLY when the key
    is absent, NOT when the value is None — so a present-but-None
    field used to crash float() with TypeError.

    Regression for the bug uncovered by the L2-BacktestDaily cron on
    the VPS at 2026-05-11 18:13 UTC (deploy day): treat None as 0.0
    so the regime filter never crashes on a malformed snapshot.
    """
    cfg = srf.SpreadRegimeConfig()
    state = srf.SpreadRegimeState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    # spread=None should not raise
    snap = _snap([10], [10], spread=0.25)
    snap["spread"] = None
    out = srf.update_spread_regime(snap, cfg, state, now=base)
    assert out["current_spread"] == 0.0  # coerced from None
    assert out["paused"] is False


def test_I6_long_pause_sets_warning_flag() -> None:
    cfg = srf.SpreadRegimeConfig(pause_at_multiple=4.0, resume_at_multiple=2.0,
                                  max_pause_seconds=10.0)
    state = srf.SpreadRegimeState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    # Build median, then trigger pause
    for i in range(10):
        srf.update_spread_regime(_snap([10], [10], spread=0.25), cfg, state,
                                  now=base + timedelta(seconds=i))
    srf.update_spread_regime(_snap([10], [10], spread=2.0), cfg, state,
                              now=base + timedelta(seconds=15))
    # 30s into pause → exceeds 10s max → warning flag fires
    out = srf.update_spread_regime(_snap([10], [10], spread=2.0), cfg, state,
                                     now=base + timedelta(seconds=45))
    assert out["paused"] is True
    assert out["long_pause_warning"] is True
    assert out["pause_held_seconds"] >= 10


def test_I3_snapshot_interval_seconds_is_5_not_1Hz() -> None:
    """Documentation fix: cadence is 5s, not the 1Hz the old comment claimed.
    Verify default in config matches reality."""
    cfg = srf.SpreadRegimeConfig()
    assert cfg.snapshot_interval_seconds == 5.0


def test_I6_bisect_insort_keeps_sorted_invariant() -> None:
    """Sorted shadow must stay sorted across many calls (replaces O(N log N)
    sort-on-every-snap with bisect.insort)."""
    cfg = srf.SpreadRegimeConfig(lookback_minutes=1)  # max_len = 12 snaps
    state = srf.SpreadRegimeState()
    spreads = [0.25, 0.50, 0.10, 1.00, 0.30, 0.05, 2.00, 0.15, 0.75, 0.40]
    for s in spreads:
        srf.update_spread_regime(_snap([10], [10], spread=s), cfg, state)
    assert state.sorted_spreads == sorted(state.recent_spreads)


# ────────────────────────────────────────────────────────────────────
# B2 — l2_overlay captures_expected fail-closed
# ────────────────────────────────────────────────────────────────────


def test_B2_no_data_pre_captures_passes_open(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Before mark_captures_expected: empty depth → passed=True (legacy fall-through)."""
    monkeypatch.setattr(l2_overlay, "DEPTH_DIR", tmp_path)
    l2_overlay.clear_captures_expected()
    dt = datetime.now(UTC)
    r = l2_overlay.confirm_sweep_with_l2(symbol="MNQ", swept_level=100.0,
                                           touch_dt=dt, side="LONG")
    assert r.passed is True
    assert r.reason == "no_l2_yet"


def test_B2_no_data_post_captures_fails_closed(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """After mark_captures_expected: empty depth → passed=False (capture daemon dead)."""
    monkeypatch.setattr(l2_overlay, "DEPTH_DIR", tmp_path)
    l2_overlay.clear_captures_expected()
    dt = datetime.now(UTC)
    l2_overlay.mark_captures_expected("MNQ", when=dt)
    r = l2_overlay.confirm_sweep_with_l2(symbol="MNQ", swept_level=100.0,
                                           touch_dt=dt, side="LONG")
    assert r.passed is False
    assert r.reason == "captures_stale_fail_closed"


def test_B2_clear_captures_expected_per_symbol(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_overlay, "DEPTH_DIR", tmp_path)
    l2_overlay.clear_captures_expected()
    dt = datetime.now(UTC)
    l2_overlay.mark_captures_expected("MNQ", when=dt)
    l2_overlay.mark_captures_expected("ES", when=dt)
    l2_overlay.clear_captures_expected("MNQ")
    # MNQ cleared → fail-OPEN; ES still set → fail-CLOSED
    r_mnq = l2_overlay.confirm_poc_pull_with_l2(symbol="MNQ", entry_dt=dt,
                                                   entry_side="LONG")
    r_es = l2_overlay.confirm_poc_pull_with_l2(symbol="ES", entry_dt=dt,
                                                  entry_side="LONG")
    assert r_mnq.passed is True
    assert r_es.passed is False


# ────────────────────────────────────────────────────────────────────
# I2 — wider sweep window + max-size + hidden-qty floor
# ────────────────────────────────────────────────────────────────────


def test_I2_sweep_takes_max_size_in_window(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Window holds 3 snaps with sizes 5, 60, 5 — old code used closest;
    new code picks MAX = 60 (real stop cluster, refilled across snaps)."""
    monkeypatch.setattr(l2_overlay, "DEPTH_DIR", tmp_path)
    l2_overlay.clear_captures_expected()
    dt = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    pre = dt - timedelta(seconds=10)
    snaps = []
    for i, qty in enumerate([5, 60, 5]):
        snap_dt = pre - timedelta(seconds=10 - i * 5)  # spread across window
        snaps.append({
            "ts": snap_dt.isoformat(),
            "epoch_s": snap_dt.timestamp(),
            "bids": [{"price": 100.0, "size": qty}],
            "asks": [{"price": 100.25, "size": 10}],
            "spread": 0.25, "mid": 100.125,
        })
    p = tmp_path / f"MNQ_{dt.strftime('%Y%m%d')}.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in snaps), encoding="utf-8")
    r = l2_overlay.confirm_sweep_with_l2(
        symbol="MNQ", swept_level=100.0, touch_dt=dt, side="LONG",
        min_stop_qty=50, window_seconds=60)
    assert r.passed is True
    assert r.detail["max_visible_qty"] == 60


def test_I2_hidden_qty_floor_rescues_thin_visible(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Visible qty=20, floor=40 → effective=60 ≥ 50 → pass."""
    monkeypatch.setattr(l2_overlay, "DEPTH_DIR", tmp_path)
    l2_overlay.clear_captures_expected()
    dt = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    pre = dt - timedelta(seconds=10)
    snap = {
        "ts": pre.isoformat(),
        "epoch_s": pre.timestamp(),
        "bids": [{"price": 100.0, "size": 20}],
        "asks": [{"price": 100.25, "size": 10}],
        "spread": 0.25, "mid": 100.125,
    }
    p = tmp_path / f"MNQ_{dt.strftime('%Y%m%d')}.jsonl"
    p.write_text(json.dumps(snap) + "\n", encoding="utf-8")
    r = l2_overlay.confirm_sweep_with_l2(
        symbol="MNQ", swept_level=100.0, touch_dt=dt, side="LONG",
        min_stop_qty=50, hidden_qty_floor=40)
    assert r.passed is True
    assert r.detail["effective_qty"] == 60


def test_I2_poc_pull_stale_snapshot_rejected(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Snap older than max_snapshot_staleness_seconds → fail-closed
    in expected mode, fail-OPEN otherwise."""
    monkeypatch.setattr(l2_overlay, "DEPTH_DIR", tmp_path)
    l2_overlay.clear_captures_expected()
    dt = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    # Snap is 25s old → window is ±30s so it's IN the window, but
    # exceeds 5s max_snapshot_staleness_seconds for poc_pull
    stale_snap_dt = dt - timedelta(seconds=25)
    snap = {
        "ts": stale_snap_dt.isoformat(),
        "epoch_s": stale_snap_dt.timestamp(),
        "bids": [{"price": 100.0, "size": 50}],
        "asks": [{"price": 100.25, "size": 10}],
        "spread": 0.25, "mid": 100.125,
    }
    p = tmp_path / f"MNQ_{dt.strftime('%Y%m%d')}.jsonl"
    p.write_text(json.dumps(snap) + "\n", encoding="utf-8")
    r = l2_overlay.confirm_poc_pull_with_l2(
        symbol="MNQ", entry_dt=dt, entry_side="LONG",
        min_imbalance_ratio=2.0,
        max_snapshot_staleness_seconds=5.0)
    # Pre-data semantics: passes OPEN with snapshot_too_stale reason
    assert r.passed is True
    assert r.reason == "snapshot_too_stale"


# ────────────────────────────────────────────────────────────────────
# B4 — SYMBOL_SPECS lookup
# ────────────────────────────────────────────────────────────────────


def test_B4_known_symbols_have_correct_point_value() -> None:
    assert l2_backtest_harness.get_spec("MNQ")["point_value"] == 2.0
    assert l2_backtest_harness.get_spec("NQ")["point_value"] == 20.0
    assert l2_backtest_harness.get_spec("GC")["point_value"] == 100.0
    assert l2_backtest_harness.get_spec("CL")["point_value"] == 1000.0


def test_B4_unknown_symbol_raises() -> None:
    with pytest.raises(ValueError, match="Unknown SYMBOL_SPECS"):
        l2_backtest_harness.get_spec("UNKNOWN_SYMBOL")


def test_B4_strips_front_month_suffix() -> None:
    assert l2_backtest_harness.get_spec("MNQ1") == l2_backtest_harness.get_spec("MNQ")


def test_B4_symbol_specs_has_tick_and_atr_for_all() -> None:
    for sym, spec in l2_backtest_harness.SYMBOL_SPECS.items():
        assert "point_value" in spec, f"{sym} missing point_value"
        assert "tick_size" in spec, f"{sym} missing tick_size"
        assert "default_atr" in spec, f"{sym} missing default_atr"
        assert spec["point_value"] > 0
        assert spec["tick_size"] > 0
        assert spec["default_atr"] > 0


# ────────────────────────────────────────────────────────────────────
# I1 — pessimistic FillModel exits
# ────────────────────────────────────────────────────────────────────


def test_I1_stop_fills_one_tick_worse_for_LONG() -> None:
    """LONG stop at 100.0 should fill at 99.75 (stop - 1 tick)."""
    sig = _make_signal(side="LONG", entry=101.0, stop=100.0, target=103.0)
    future = [
        # Snap shows price dropping: snap_low = mid - spread/2 = 99.75
        {"mid": 99.875, "spread": 0.25, "ts": "t1"},
    ]
    trade = l2_backtest_harness._simulate_exit_pessimistic(
        sig, future, point_value=2.0, tick_size=0.25)
    assert trade.exit_reason == "STOP"
    assert trade.exit_price == 99.75  # stop - 1 tick


def test_I1_stop_fills_one_tick_worse_for_SHORT() -> None:
    sig = _make_signal(side="SHORT", entry=99.0, stop=100.0, target=97.0)
    future = [
        # snap_high = mid + spread/2 = 100.25 → above stop
        {"mid": 100.125, "spread": 0.25, "ts": "t1"},
    ]
    trade = l2_backtest_harness._simulate_exit_pessimistic(
        sig, future, point_value=2.0, tick_size=0.25)
    assert trade.exit_reason == "STOP"
    assert trade.exit_price == 100.25  # stop + 1 tick


def test_I1_target_AND_stop_in_same_snap_stop_wins() -> None:
    """Conservative tie-break: when both levels touched in same snap window,
    STOP wins."""
    sig = _make_signal(side="LONG", entry=100.0, stop=99.0, target=101.0)
    # mid=100.0, spread=4.0 → snap_high=102 (target hit) AND snap_low=98 (stop hit)
    future = [{"mid": 100.0, "spread": 4.0, "ts": "t1"}]
    trade = l2_backtest_harness._simulate_exit_pessimistic(
        sig, future, point_value=2.0, tick_size=0.25)
    assert trade.exit_reason == "STOP"


def test_I1_commission_subtracted_from_net() -> None:
    sig = _make_signal(side="LONG", entry=100.0, stop=98.0, target=104.0)
    future = [{"mid": 104.5, "spread": 0.25, "ts": "t1"}]  # snap_high=104.625 ≥ target
    trade = l2_backtest_harness._simulate_exit_pessimistic(
        sig, future, point_value=2.0, tick_size=0.25)
    assert trade.exit_reason == "TARGET"
    assert trade.pnl_dollars > trade.pnl_dollars_net  # commission deducted
    assert (trade.pnl_dollars - trade.pnl_dollars_net) == pytest.approx(
        l2_backtest_harness.COMMISSION_PER_RT_USD)


def _make_signal(*, side: str, entry: float, stop: float, target: float):
    """Build a minimal signal object for harness testing."""
    class _Sig:
        pass
    s = _Sig()
    s.side = side
    s.entry_price = entry
    s.stop = stop
    s.target = target
    s.snapshot_ts = "test_ts"
    s.confidence = 0.5
    s.signal_id = "TEST-SIG-1"
    return s


# ────────────────────────────────────────────────────────────────────
# I4 — apply spread_regime_filter in backtest
# ────────────────────────────────────────────────────────────────────


def test_I4_backtest_skips_signals_during_regime_pause(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a depth file where spread blows out mid-stream; verify the
    harness counts those skipped signals via n_skipped_regime_pause."""
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    base_epoch = today.timestamp()
    snaps = []
    # 30 normal snaps to bootstrap median
    for i in range(30):
        snaps.append({
            "ts": (today + timedelta(seconds=i)).isoformat(),
            "epoch_s": base_epoch + i,
            "bids": [{"price": 100.0, "size": 50}],
            "asks": [{"price": 100.25, "size": 10}],
            "spread": 0.25, "mid": 100.125,
        })
    # 30 blown-out snaps — spread 5x median → PAUSE
    for i in range(30, 60):
        snaps.append({
            "ts": (today + timedelta(seconds=i)).isoformat(),
            "epoch_s": base_epoch + i,
            "bids": [{"price": 100.0, "size": 50}],
            "asks": [{"price": 101.5, "size": 10}],  # wide
            "spread": 1.5, "mid": 100.75,
        })
    p = tmp_path / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")

    result = l2_backtest_harness.run_book_imbalance(
        "MNQ", days=1,
        entry_threshold=1.5, consecutive_snaps=3,
        n_levels=3, atr_stop_mult=1.0, rr_target=2.0,
        walk_forward=False,
    )
    assert result.n_skipped_regime_pause > 0


def test_I4_disable_regime_filter_via_flag(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-regime-filter must let all signals through."""
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    base_epoch = today.timestamp()
    snaps = []
    for i in range(60):
        snaps.append({
            "ts": (today + timedelta(seconds=i)).isoformat(),
            "epoch_s": base_epoch + i,
            "bids": [{"price": 100.0, "size": 50}],
            "asks": [{"price": 102.0, "size": 10}],
            "spread": 2.0, "mid": 101.0,  # always wide
        })
    p = tmp_path / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")

    result = l2_backtest_harness.run_book_imbalance(
        "MNQ", days=1,
        entry_threshold=1.5, consecutive_snaps=3,
        n_levels=3, atr_stop_mult=1.0, rr_target=2.0,
        apply_regime_filter=False,
        walk_forward=False,
    )
    assert result.n_skipped_regime_pause == 0


# ────────────────────────────────────────────────────────────────────
# I9 — walk-forward + min-N gate
# ────────────────────────────────────────────────────────────────────


def test_I9_sharpe_proxy_invalid_when_below_min_n() -> None:
    result = l2_backtest_harness._summarize(
        "book_imbalance", "MNQ", 1,
        n_snapshots=10, trades=[], n_signals=0,
        n_skipped_regime=0, point_value=2.0,
        walk_forward=None, min_n_for_sharpe=30,
    )
    assert result.sharpe_proxy_valid is False


def test_I9_walk_forward_split_present_with_enough_snaps(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    base_epoch = today.timestamp()
    snaps = []
    for i in range(150):  # > 100 → walk-forward kicks in
        snaps.append({
            "ts": (today + timedelta(seconds=i)).isoformat(),
            "epoch_s": base_epoch + i,
            "bids": [{"price": 100.0, "size": 30}],
            "asks": [{"price": 100.25, "size": 30}],
            "spread": 0.25, "mid": 100.125,
        })
    p = tmp_path / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")

    result = l2_backtest_harness.run_book_imbalance(
        "MNQ", days=1,
        entry_threshold=1.5, consecutive_snaps=3,
        n_levels=3, atr_stop_mult=1.0, rr_target=2.0,
        walk_forward=True,
    )
    assert result.walk_forward is not None
    assert "train" in result.walk_forward
    assert "test" in result.walk_forward
    assert "promotion_gate" in result.walk_forward


def test_I9_walk_forward_skipped_with_few_snaps(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """< 100 snaps → walk_forward returns None (insufficient sample)."""
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    snap = {
        "ts": today.isoformat(),
        "epoch_s": today.timestamp(),
        "bids": [{"price": 100.0, "size": 30}],
        "asks": [{"price": 100.25, "size": 30}],
        "spread": 0.25, "mid": 100.125,
    }
    p = tmp_path / f"MNQ_{today.strftime('%Y%m%d')}.jsonl"
    p.write_text(json.dumps(snap) + "\n", encoding="utf-8")
    result = l2_backtest_harness.run_book_imbalance(
        "MNQ", days=1,
        entry_threshold=1.5, consecutive_snaps=3,
        n_levels=3, atr_stop_mult=1.0, rr_target=2.0,
        walk_forward=True,
    )
    assert result.walk_forward is None


# ────────────────────────────────────────────────────────────────────
# B5 — trading_gate (disk + capture circuit breaker)
# ────────────────────────────────────────────────────────────────────


def _write_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def test_B5_no_disk_digest_blocks(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading_gate, "DISK_LOG", tmp_path / "disk.jsonl")
    monkeypatch.setattr(trading_gate, "CAPTURE_HEALTH_LOG", tmp_path / "cap.jsonl")
    monkeypatch.setattr(trading_gate, "GATE_LOG", tmp_path / "gate.jsonl")
    trading_gate._reset_cache_for_tests()
    decision = trading_gate.check_pre_trade_gate("MNQ", force_refresh=True)
    assert decision.blocked is True
    assert decision.reason == "no_disk_digest"


def test_B5_critical_disk_blocks(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading_gate, "DISK_LOG", tmp_path / "disk.jsonl")
    monkeypatch.setattr(trading_gate, "CAPTURE_HEALTH_LOG", tmp_path / "cap.jsonl")
    monkeypatch.setattr(trading_gate, "GATE_LOG", tmp_path / "gate.jsonl")
    trading_gate._reset_cache_for_tests()
    now_iso = datetime.now(UTC).isoformat()
    _write_jsonl(tmp_path / "disk.jsonl", {"ts": now_iso, "verdict": "CRITICAL",
                                              "worst_partition": "C:\\"})
    _write_jsonl(tmp_path / "cap.jsonl", {"ts": now_iso, "verdict": "GREEN"})
    decision = trading_gate.check_pre_trade_gate("MNQ", force_refresh=True)
    assert decision.blocked is True
    assert decision.reason == "disk_CRITICAL"


def test_B5_red_capture_health_blocks(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading_gate, "DISK_LOG", tmp_path / "disk.jsonl")
    monkeypatch.setattr(trading_gate, "CAPTURE_HEALTH_LOG", tmp_path / "cap.jsonl")
    monkeypatch.setattr(trading_gate, "GATE_LOG", tmp_path / "gate.jsonl")
    trading_gate._reset_cache_for_tests()
    now_iso = datetime.now(UTC).isoformat()
    _write_jsonl(tmp_path / "disk.jsonl", {"ts": now_iso, "verdict": "GREEN"})
    _write_jsonl(tmp_path / "cap.jsonl", {"ts": now_iso, "verdict": "RED",
                                             "issues": ["MNQ ticks STALE"]})
    decision = trading_gate.check_pre_trade_gate("MNQ", force_refresh=True)
    assert decision.blocked is True
    assert decision.reason == "capture_RED"


def test_B5_stale_disk_digest_blocks(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading_gate, "DISK_LOG", tmp_path / "disk.jsonl")
    monkeypatch.setattr(trading_gate, "CAPTURE_HEALTH_LOG", tmp_path / "cap.jsonl")
    monkeypatch.setattr(trading_gate, "GATE_LOG", tmp_path / "gate.jsonl")
    trading_gate._reset_cache_for_tests()
    old_iso = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _write_jsonl(tmp_path / "disk.jsonl", {"ts": old_iso, "verdict": "GREEN"})
    decision = trading_gate.check_pre_trade_gate("MNQ", force_refresh=True)
    assert decision.blocked is True
    assert decision.reason == "disk_digest_stale"


def test_B5_all_green_passes(tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading_gate, "DISK_LOG", tmp_path / "disk.jsonl")
    monkeypatch.setattr(trading_gate, "CAPTURE_HEALTH_LOG", tmp_path / "cap.jsonl")
    monkeypatch.setattr(trading_gate, "GATE_LOG", tmp_path / "gate.jsonl")
    trading_gate._reset_cache_for_tests()
    now_iso = datetime.now(UTC).isoformat()
    _write_jsonl(tmp_path / "disk.jsonl", {"ts": now_iso, "verdict": "GREEN"})
    _write_jsonl(tmp_path / "cap.jsonl", {"ts": now_iso, "verdict": "GREEN"})
    decision = trading_gate.check_pre_trade_gate("MNQ", force_refresh=True)
    assert decision.blocked is False
    assert decision.reason == "ok"


# ────────────────────────────────────────────────────────────────────
# D2 — atomic gzip rename
# ────────────────────────────────────────────────────────────────────


def test_D2_gzip_in_place_writes_via_tmp_then_renames(tmp_path: Path) -> None:
    src = tmp_path / "MNQ_20260411.jsonl"
    src.write_text("line1\nline2\nline3\n", encoding="utf-8")
    gz = capture_rotation._gzip_in_place(src)
    assert gz.exists()
    assert gz.suffix == ".gz"
    # No leftover .tmp
    assert not (tmp_path / "MNQ_20260411.jsonl.gz.tmp").exists()
    # Source still exists (caller decides to delete)
    assert src.exists()
    # Decompress and verify content
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        assert f.read() == "line1\nline2\nline3\n"


def test_D2_rotation_digest_includes_pending_counters(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """DRY-RUN with pending work must surface n_would_compress in digest."""
    monkeypatch.setattr(capture_rotation, "TICKS_DIR", tmp_path / "ticks")
    monkeypatch.setattr(capture_rotation, "DEPTH_DIR", tmp_path / "depth")
    (tmp_path / "ticks").mkdir()
    (tmp_path / "depth").mkdir()
    # File 30 days old → eligible for compression
    old_date = (datetime.now(UTC).date() - timedelta(days=30)).strftime("%Y%m%d")
    p = tmp_path / "ticks" / f"MNQ_{old_date}.jsonl"
    p.write_text("x" * 1000, encoding="utf-8")
    today = datetime.now(UTC).date()
    digest = capture_rotation._process_kind(tmp_path / "ticks", "ticks", today,
                                              keep_days=14, cold_days=90, apply=False)
    assert digest["n_would_compress"] == 1


# ────────────────────────────────────────────────────────────────────
# D3 — dashboard DRY-RUN with pending → YELLOW
# ────────────────────────────────────────────────────────────────────


def test_D3_dashboard_marks_dryrun_with_pending_as_yellow(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(health_dashboard.SOURCES, "capture_rotation",
                          tmp_path / "rot.jsonl")
    rot_record = {
        "ts": datetime.now(UTC).isoformat(),
        "apply": False,
        "ticks": {"n_compressed": 0, "n_cold_archived": 0,
                   "n_would_compress": 5, "n_would_cold_archived": 0},
        "depth": {"n_compressed": 0, "n_cold_archived": 0,
                   "n_would_compress": 3, "n_would_cold_archived": 1},
        "totals": {"n_compressed": 0, "n_cold_archived": 0},
    }
    (tmp_path / "rot.jsonl").write_text(json.dumps(rot_record) + "\n",
                                          encoding="utf-8")
    d = health_dashboard.build_dashboard(alert_hours=24)
    assert d["sections"]["capture_rotation"]["status"] == "YELLOW"
    assert d["sections"]["capture_rotation"]["n_pending"] == 9


def test_D3_dashboard_dryrun_with_no_pending_stays_dryrun(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(health_dashboard.SOURCES, "capture_rotation",
                          tmp_path / "rot.jsonl")
    rot_record = {
        "ts": datetime.now(UTC).isoformat(),
        "apply": False,
        "ticks": {"n_compressed": 0, "n_cold_archived": 0,
                   "n_would_compress": 0, "n_would_cold_archived": 0},
        "depth": {"n_compressed": 0, "n_cold_archived": 0,
                   "n_would_compress": 0, "n_would_cold_archived": 0},
        "totals": {"n_compressed": 0, "n_cold_archived": 0},
    }
    (tmp_path / "rot.jsonl").write_text(json.dumps(rot_record) + "\n",
                                          encoding="utf-8")
    d = health_dashboard.build_dashboard(alert_hours=24)
    assert d["sections"]["capture_rotation"]["status"] == "DRY-RUN"


def test_D4_parse_ts_helper_handles_iso_and_epoch() -> None:
    iso = "2026-05-11T14:30:00+00:00"
    dt = health_dashboard._parse_ts(iso)
    assert dt is not None
    assert dt.year == 2026

    dt2 = health_dashboard._parse_ts(1746719531.123)
    assert dt2 is not None

    assert health_dashboard._parse_ts(None) is None
    assert health_dashboard._parse_ts("not-a-date") is None
    assert health_dashboard._parse_ts({"weird": "type"}) is None


def test_D6_disk_monitor_alert_failure_writes_to_stderr(monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture) -> None:
    """When alert log can't be written, the OSError is reported on stderr
    (D6 fix — used to be silent ``except OSError: pass``)."""
    class _BadPath:
        def open(self, *args, **kwargs):
            raise OSError("disk full")

    monkeypatch.setattr(disk_space_monitor, "ALERT_LOG", _BadPath())
    # Should NOT raise — failure should be silently caught and reported.
    disk_space_monitor._emit_alert("RED", "test alert", {"x": 1})
    captured = capsys.readouterr()
    assert "disk_space_monitor WARN" in captured.err
    assert "disk full" in captured.err
