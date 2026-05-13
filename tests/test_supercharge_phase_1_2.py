# ruff: noqa: N802, PLR2004
"""Tests for the supercharge Phase 1 + Phase 2 deliverables:

Phase 1 (foundation):
- E: depth_simulator.py — synthetic depth generator
- D: bootstrap CI, deflated sharpe, config audit log in l2_backtest_harness

Phase 2 (strategies):
- C1: footprint_absorption_strategy
- C2: aggressor_flow_strategy
- C3: microprice_drift_strategy
- B1: sweep_reclaim_strategy v2 L2 overlay
- B2: volume_profile_strategy v2 L2 overlay
- B3: anchor_sweep_strategy v2 L2 overlay
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from eta_engine.scripts import depth_simulator, l2_backtest_harness
from eta_engine.strategies import (
    aggressor_flow_strategy as agg,
)
from eta_engine.strategies import (
    footprint_absorption_strategy as fp,
)
from eta_engine.strategies import (
    microprice_drift_strategy as mp,
)

if TYPE_CHECKING:
    import pytest


# ────────────────────────────────────────────────────────────────────
# E — depth_simulator
# ────────────────────────────────────────────────────────────────────


def test_E_simulator_produces_expected_count() -> None:
    """30 min * 60 / 5 = 360 snaps for default cadence."""
    snaps, _ = depth_simulator.simulate(symbol="MNQ", duration_minutes=30, snapshot_interval_seconds=5.0, seed=42)
    assert len(snaps) == 360


def test_E_simulator_schema_matches_live_capture() -> None:
    """Every snap must have the keys the live capture produces."""
    snaps, _ = depth_simulator.simulate(symbol="MNQ", duration_minutes=5, seed=42)
    required_top = {"ts", "epoch_s", "symbol", "bids", "asks", "spread", "mid"}
    required_lvl = {"price", "size", "mm"}
    for s in snaps:
        assert required_top.issubset(s.keys())
        assert s["bids"] and required_lvl.issubset(s["bids"][0].keys())
        assert s["asks"] and required_lvl.issubset(s["asks"][0].keys())
        assert s["spread"] > 0
        assert s["mid"] > 0


def test_E_simulator_seed_is_deterministic() -> None:
    a, _ = depth_simulator.simulate(symbol="MNQ", duration_minutes=5, seed=42)
    b, _ = depth_simulator.simulate(symbol="MNQ", duration_minutes=5, seed=42)
    assert [s["mid"] for s in a] == [s["mid"] for s in b]


def test_E_imbalanced_long_has_more_bid_qty_than_ask() -> None:
    snaps, _ = depth_simulator.simulate(symbol="MNQ", duration_minutes=10, regime_mix="imbalanced_long", seed=42)
    bid_total = sum(sum(lv["size"] for lv in s["bids"]) for s in snaps)
    ask_total = sum(sum(lv["size"] for lv in s["asks"]) for s in snaps)
    assert bid_total > ask_total * 1.5  # at least 50% more bids


def test_E_stressed_mix_has_wider_spreads() -> None:
    calm, _ = depth_simulator.simulate(symbol="MNQ", duration_minutes=10, regime_mix="calm", seed=1)
    stressed, _ = depth_simulator.simulate(symbol="MNQ", duration_minutes=10, regime_mix="stressed", seed=1)
    calm_mean = sum(s["spread"] for s in calm) / len(calm)
    stressed_mean = sum(s["spread"] for s in stressed) / len(stressed)
    assert stressed_mean > calm_mean


def test_E_write_snapshots_creates_jsonl(tmp_path: Path) -> None:
    snaps, _ = depth_simulator.simulate(symbol="MNQ", duration_minutes=5, seed=42)
    path = depth_simulator.write_snapshots(snaps, "MNQ", output_dir=tmp_path)
    assert path.exists()
    assert path.suffix == ".jsonl"
    # Each line is valid JSON
    lines = path.read_text().splitlines()
    assert len(lines) == len(snaps)
    for line in lines[:3]:
        parsed = json.loads(line)
        assert "ts" in parsed


# ────────────────────────────────────────────────────────────────────
# D — bootstrap CI, deflated sharpe, config audit
# ────────────────────────────────────────────────────────────────────


def test_D_bootstrap_ci_returns_none_below_min_n() -> None:
    assert l2_backtest_harness.bootstrap_ci([1.0, 2.0]) is None
    assert l2_backtest_harness.bootstrap_ci([1.0, 2.0, 3.0, 4.0]) is None


def test_D_bootstrap_ci_returns_bounds_for_adequate_sample() -> None:
    values = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5]
    ci = l2_backtest_harness.bootstrap_ci(values, seed=42)
    assert ci is not None
    lo, hi = ci
    assert lo < hi
    assert 2.0 < lo < 4.0  # lower bound should be below the mean
    assert 3.0 < hi < 5.0  # upper bound above the mean


def test_D_sharpe_ci_handles_zero_volatility() -> None:
    # All trades return exactly +1 → sharpe is infinity in theory
    # but bootstrap should handle it gracefully (returns 0 when std=0)
    ci = l2_backtest_harness.bootstrap_sharpe_ci([1.0] * 10, seed=42)
    assert ci is not None
    lo, hi = ci
    assert lo == hi == 0.0  # division-by-zero short-circuit


def test_D_deflated_sharpe_no_correction_when_n_trials_eq_1() -> None:
    assert l2_backtest_harness.deflated_sharpe_ratio(1.5, n_trials=1, n_trades=100) == 1.5


def test_D_deflated_sharpe_corrects_downward_when_many_trials() -> None:
    # With 100 trials and 50 trades, observed sharpe should deflate
    observed = 2.0
    deflated = l2_backtest_harness.deflated_sharpe_ratio(observed, n_trials=100, n_trades=50)
    assert deflated < observed
    assert deflated > 0  # but not flipped negative for a 2.0 observed


def test_D_config_search_log_writes_and_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = tmp_path / "config_search.jsonl"
    monkeypatch.setattr(l2_backtest_harness, "CONFIG_SEARCH_LOG", log_path)
    l2_backtest_harness.log_config_search(
        strategy="book_imbalance",
        symbol="MNQ",
        days=7,
        config={"entry_threshold": 1.5, "consecutive_snaps": 3},
        n_trades=10,
        sharpe_proxy=0.5,
        sharpe_proxy_valid=False,
        win_rate=0.6,
        total_pnl_dollars_net=100.0,
    )
    l2_backtest_harness.log_config_search(
        strategy="book_imbalance",
        symbol="MNQ",
        days=7,
        config={"entry_threshold": 1.75, "consecutive_snaps": 3},
        n_trades=8,
        sharpe_proxy=0.3,
        sharpe_proxy_valid=False,
        win_rate=0.55,
        total_pnl_dollars_net=50.0,
    )
    n_searched = l2_backtest_harness.count_prior_configs_searched("book_imbalance", "MNQ")
    assert n_searched == 2


def test_D_norm_ppf_returns_expected_quantiles() -> None:
    # Known checks: ppf(0.5) ≈ 0; ppf(0.975) ≈ 1.96
    assert abs(l2_backtest_harness._norm_ppf(0.5)) < 0.01
    assert abs(l2_backtest_harness._norm_ppf(0.975) - 1.96) < 0.05


# ────────────────────────────────────────────────────────────────────
# C1 — footprint_absorption_strategy
# ────────────────────────────────────────────────────────────────────


def test_C1_footprint_fires_on_absorbed_buy_print() -> None:
    cfg = fp.FootprintAbsorptionConfig(
        prints_size_z_min=1.0,
        absorption_ratio=0.5,
        absorb_price_band_ticks=2.0,
        cooldown_seconds=0.0,
    )
    state = fp.FootprintAbsorptionState()
    # Build varied baseline so std > 0 (z-score requires non-zero std)
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    rng = [3, 4, 5, 6, 7, 4, 5, 6, 8, 4, 5, 6, 7, 5, 4, 5, 6, 5, 7, 4]
    for i, s in enumerate(rng):
        fp.record_print(
            state,
            price=100.0,
            size=float(s),
            side="BUY",
            ts=base + timedelta(seconds=i),
            mid_before=100.0,
            mid_after=100.05,
            opposite_qty_before=20,
            opposite_qty_after=18,
        )
    # Large absorbed buy print
    fp.record_print(
        state,
        price=100.0,
        size=50.0,
        side="BUY",
        ts=base + timedelta(seconds=25),
        mid_before=100.0,
        mid_after=100.05,
        opposite_qty_before=100,
        opposite_qty_after=95,
    )
    sig = fp.evaluate_footprint(state, cfg, atr=2.0, symbol="MNQ")
    assert sig is not None
    assert sig.side == "SHORT"
    assert sig.signal_id.startswith("MNQ-FOOTPRINT-SHORT-")


def test_C1_footprint_no_signal_when_opp_qty_drops_a_lot() -> None:
    cfg = fp.FootprintAbsorptionConfig(prints_size_z_min=1.0, cooldown_seconds=0.0)
    state = fp.FootprintAbsorptionState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    for i in range(20):
        fp.record_print(
            state,
            price=100.0,
            size=5.0,
            side="BUY",
            ts=base + timedelta(seconds=i),
            mid_before=100.0,
            mid_after=100.05,
            opposite_qty_before=20,
            opposite_qty_after=18,
        )
    # Large print but bid side DROPS a lot — not absorbed
    fp.record_print(
        state,
        price=100.0,
        size=50.0,
        side="BUY",
        ts=base + timedelta(seconds=25),
        mid_before=100.0,
        mid_after=100.5,  # also price moved
        opposite_qty_before=100,
        opposite_qty_after=30,
    )
    sig = fp.evaluate_footprint(state, cfg, atr=2.0, symbol="MNQ")
    assert sig is None


def test_C1_footprint_respects_cooldown() -> None:
    cfg = fp.FootprintAbsorptionConfig(prints_size_z_min=1.0, cooldown_seconds=120.0)
    state = fp.FootprintAbsorptionState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    rng = [3, 4, 5, 6, 7, 4, 5, 6, 8, 4, 5, 6, 7, 5, 4, 5, 6, 5, 7, 4]
    for i, s in enumerate(rng):
        fp.record_print(
            state,
            price=100.0,
            size=float(s),
            side="BUY",
            ts=base + timedelta(seconds=i),
            mid_before=100.0,
            mid_after=100.05,
            opposite_qty_before=20,
            opposite_qty_after=18,
        )
    fp.record_print(
        state,
        price=100.0,
        size=50.0,
        side="BUY",
        ts=base + timedelta(seconds=25),
        mid_before=100.0,
        mid_after=100.05,
        opposite_qty_before=100,
        opposite_qty_after=95,
    )
    sig1 = fp.evaluate_footprint(state, cfg, atr=2.0, symbol="MNQ")
    assert sig1 is not None
    # Immediate second large print — should be blocked by cooldown
    fp.record_print(
        state,
        price=100.0,
        size=50.0,
        side="BUY",
        ts=base + timedelta(seconds=30),
        mid_before=100.0,
        mid_after=100.05,
        opposite_qty_before=100,
        opposite_qty_after=95,
    )
    sig2 = fp.evaluate_footprint(state, cfg, atr=2.0, symbol="MNQ")
    assert sig2 is None


# ────────────────────────────────────────────────────────────────────
# C2 — aggressor_flow_strategy
# ────────────────────────────────────────────────────────────────────


def _bar(*, ts: datetime, close: float, volume_buy: float, volume_sell: float, open_: float | None = None) -> dict:
    return {
        "timestamp_utc": ts.isoformat(),
        "epoch_s": ts.timestamp(),
        "open": open_ if open_ is not None else close - 0.1,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume_total": volume_buy + volume_sell,
        "volume_buy": volume_buy,
        "volume_sell": volume_sell,
        "n_trades": 100,
    }


def test_C2_aggressor_flow_fires_long_on_sustained_buying() -> None:
    cfg = agg.AggressorFlowConfig(
        window_bars=5,
        entry_threshold=0.30,
        consecutive_bars=2,
        cooldown_seconds=0.0,
        require_close_confirm=True,
    )
    state = agg.AggressorFlowState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    # 5 bars with heavy buy aggressor (buy 80 / sell 20 = ratio 0.60)
    for i in range(5):
        b = _bar(
            ts=base + timedelta(minutes=5 * i),
            close=100.0 + i * 0.5,
            open_=100.0 + i * 0.5 - 0.3,
            volume_buy=80,
            volume_sell=20,
        )
        agg.evaluate_bar(b, cfg, state, atr=2.0, symbol="MNQ")
    # Next bar — should fire LONG (consecutive_bars=2 met)
    b = _bar(ts=base + timedelta(minutes=30), close=103.0, open_=102.5, volume_buy=80, volume_sell=20)
    sig = agg.evaluate_bar(b, cfg, state, atr=2.0, symbol="MNQ")
    assert sig is not None
    assert sig.side == "LONG"


def test_C2_aggressor_flow_fires_short_on_sustained_selling() -> None:
    cfg = agg.AggressorFlowConfig(
        window_bars=5, entry_threshold=0.30, consecutive_bars=1, cooldown_seconds=0.0, require_close_confirm=True
    )
    state = agg.AggressorFlowState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    # Build window with selling pressure (sell 80 / buy 20 = ratio -0.60)
    for i in range(6):
        b = _bar(
            ts=base + timedelta(minutes=5 * i),
            close=100.0 - i * 0.5,
            open_=100.0 - i * 0.5 + 0.3,
            volume_buy=20,
            volume_sell=80,
        )
        sig = agg.evaluate_bar(b, cfg, state, atr=2.0, symbol="MNQ")
    assert sig is not None
    assert sig.side == "SHORT"


def test_C2_compute_imbalance_ratio_zero_on_empty() -> None:
    ratio, b, s = agg.compute_imbalance_ratio([])
    assert ratio == 0.0
    assert b == 0
    assert s == 0


# ────────────────────────────────────────────────────────────────────
# C3 — microprice_drift_strategy
# ────────────────────────────────────────────────────────────────────


def _snap(bid_price: float, bid_qty: int, ask_price: float, ask_qty: int, *, ts: datetime | None = None) -> dict:
    ts_dt = ts or datetime.now(UTC)
    return {
        "ts": ts_dt.isoformat(),
        "epoch_s": ts_dt.timestamp(),
        "bids": [{"price": bid_price, "size": bid_qty, "mm": "TEST"}],
        "asks": [{"price": ask_price, "size": ask_qty, "mm": "TEST"}],
        "spread": ask_price - bid_price,
        "mid": (ask_price + bid_price) / 2,
    }


def test_C3_compute_microprice_weights_by_opposite_side() -> None:
    # bid=100.0/100, ask=100.5/10  → micro should be near 100.0
    # (because ask qty is tiny → market wants down)
    micro, mid, cls = mp.compute_microprice(_snap(100.0, 100, 100.5, 10))
    assert cls == "OK"
    assert micro is not None and mid is not None
    # micro = (100.0 * 10 + 100.5 * 100) / 110 ≈ 100.455
    expected = (100.0 * 10 + 100.5 * 100) / 110
    assert abs(micro - expected) < 0.001


def test_C3_microprice_empty_book_classifies() -> None:
    snap_empty = {"bids": [], "asks": [{"price": 100.5, "size": 10}], "spread": 0, "mid": 0}
    micro, mid, cls = mp.compute_microprice(snap_empty)
    assert cls == "EMPTY_BIDS"
    assert micro is None


def test_C3_microprice_fires_long_on_upward_drift() -> None:
    cfg = mp.MicropriceConfig(
        drift_threshold_ticks=1.0,
        consecutive_snaps=2,
        cooldown_seconds=0.0,
        snapshot_interval_seconds=5.0,
    )
    state = mp.MicropriceState()
    base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    mp.update_trade_price(state, 100.0)
    # Collect ALL signals across the loop — first 2 consecutive snaps
    # build count, 2nd fires (and resets counter), then more snaps
    # build again.  Track which one fired.
    signals: list[mp.MicropriceSignal] = []
    for i in range(5):
        snap = _snap(100.0, 100, 100.5, 5, ts=base + timedelta(seconds=5 * i))
        sig = mp.evaluate_snapshot(snap, cfg, state, atr=2.0, symbol="MNQ")
        if sig is not None:
            signals.append(sig)
    assert len(signals) >= 1
    assert signals[0].side == "LONG"
    assert signals[0].signal_id.startswith("MNQ-MICRO-LONG-")


def test_C3_microprice_no_signal_without_trade_price() -> None:
    cfg = mp.MicropriceConfig(drift_threshold_ticks=1.0, consecutive_snaps=1, cooldown_seconds=0.0)
    state = mp.MicropriceState()
    # No update_trade_price call → state.last_trade_price is None
    sig = mp.evaluate_snapshot(_snap(100.0, 100, 100.5, 5), cfg, state)
    assert sig is None


# ────────────────────────────────────────────────────────────────────
# E2E — depth_simulator + harness integration
# ────────────────────────────────────────────────────────────────────


def test_E2E_simulator_into_harness_produces_signals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Imbalanced regime + book_imbalance strategy = signals fire."""
    monkeypatch.setattr(l2_backtest_harness, "DEPTH_DIR", tmp_path)
    today = datetime.now(UTC).replace(microsecond=0, second=0)
    snaps, _ = depth_simulator.simulate(
        symbol="MNQ", duration_minutes=30, regime_mix="imbalanced_long", seed=42, start_dt=today
    )
    depth_simulator.write_snapshots(snaps, "MNQ", output_dir=tmp_path, date_str=today.strftime("%Y%m%d"))
    result = l2_backtest_harness.run_book_imbalance(
        "MNQ",
        days=1,
        entry_threshold=1.5,
        consecutive_snaps=3,
        n_levels=3,
        atr_stop_mult=1.0,
        rr_target=2.0,
        walk_forward=False,
        log_config_search_flag=False,
    )
    assert result.n_snapshots == 360
    # Should fire at least 1 signal in imbalanced regime
    assert result.n_signals >= 1
