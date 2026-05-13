"""Hardening battery for the 3 newer L2 strategies — footprint
absorption, aggressor flow, microprice drift.

Coverage parity with book_imbalance's test_l2_hardening_fixes.py
across the relevant B/I codes:

  - B3  cooldown enforcement (seconds-based for these 3, vs bars for
        book_imbalance)
  - B6  signal_id idempotency + hard-capped qty + min_stop_ticks floor
  - I5  tick_size respected per-symbol (tunable)
  - I7  gap-aware reset (only microprice has gap_reset_multiple — the
        others use cooldown_seconds which subsumes gap protection)
  - I8  zero/empty book + None-valued field fail-CLOSED
  - I9  max_trades_per_day cap

Plus the None-safety regressions caught during the 2026-05-11 deploy
that motivated the spread/mid coercion fixes for spread_regime_filter,
book_imbalance, and l2_backtest_harness.  Same root cause as those
fixes — ``dict.get(key, default)`` returns the default ONLY when the
key is absent, not when the value is None.
"""

# ruff: noqa: N802, PLR2004
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.strategies import (
    aggressor_flow_strategy as ag,
)
from eta_engine.strategies import (
    footprint_absorption_strategy as fp,
)
from eta_engine.strategies import (
    microprice_drift_strategy as mp,
)

# ────────────────────────────────────────────────────────────────────
# Test fixtures
# ────────────────────────────────────────────────────────────────────


def _print_record(
    *,
    size: float,
    side: str = "BUY",
    mid_before: float = 100.0,
    mid_after: float = 100.0,
    opp_qty_before: int = 10,
    opp_qty_after: int = 8,
    ts: datetime | None = None,
) -> dict:
    return {
        "ts": ts or datetime(2026, 5, 12, 14, 30, 0, tzinfo=UTC),
        "price": mid_after,
        "size": size,
        "side": side,
        "mid_before": mid_before,
        "mid_after": mid_after,
        "opposite_qty_before": opp_qty_before,
        "opposite_qty_after": opp_qty_after,
    }


def _bar(*, ts: datetime, buy: float = 50, sell: float = 50, close: float = 100.0, open_: float = 99.5) -> dict:
    return {
        "timestamp_utc": ts.isoformat(),
        "epoch_s": ts.timestamp(),
        "open": open_,
        "high": max(open_, close) + 0.5,
        "low": min(open_, close) - 0.5,
        "close": close,
        "volume_total": buy + sell,
        "volume_buy": buy,
        "volume_sell": sell,
        "n_trades": 10,
    }


def _depth_snap(
    *,
    bid_price: float = 99.75,
    ask_price: float = 100.25,
    bid_qty: int = 10,
    ask_qty: int = 10,
    spread: float = 0.5,
    mid: float = 100.0,
    ts: datetime | None = None,
) -> dict:
    ts = ts or datetime(2026, 5, 12, 14, 30, 0, tzinfo=UTC)
    return {
        "ts": ts.isoformat(),
        "epoch_s": ts.timestamp(),
        "bids": [{"price": bid_price, "size": bid_qty}],
        "asks": [{"price": ask_price, "size": ask_qty}],
        "spread": spread,
        "mid": mid,
    }


# ────────────────────────────────────────────────────────────────────
# footprint_absorption  -- None safety
# ────────────────────────────────────────────────────────────────────


def test_footprint_none_safety_size_field() -> None:
    """size=None must not crash — should be treated as 0 and skipped."""
    state = fp.FootprintAbsorptionState()
    cfg = fp.FootprintAbsorptionConfig()
    state.recent_prints.append(_print_record(size=100))
    # Now overwrite size to None
    state.recent_prints[-1]["size"] = None
    sig = fp.evaluate_footprint(state, cfg, atr=2.0)
    assert sig is None


def test_footprint_none_safety_opp_qty_fields() -> None:
    """opposite_qty_before / opposite_qty_after = None must not crash."""
    state = fp.FootprintAbsorptionState()
    cfg = fp.FootprintAbsorptionConfig(prints_size_z_min=0.0)
    # Populate history
    for _ in range(15):
        state.recent_prints.append(_print_record(size=10))
    rec = _print_record(size=100)
    rec["opposite_qty_before"] = None
    rec["opposite_qty_after"] = None
    state.recent_prints.append(rec)
    sig = fp.evaluate_footprint(state, cfg, atr=2.0)
    # Should not crash; signal may be None or valid — what matters is no exception.
    assert sig is None or isinstance(sig, fp.FootprintSignal)


def test_footprint_none_safety_mid_fields() -> None:
    """mid_before / mid_after = None must not crash — return None."""
    state = fp.FootprintAbsorptionState()
    cfg = fp.FootprintAbsorptionConfig(prints_size_z_min=0.0)
    for _ in range(15):
        state.recent_prints.append(_print_record(size=10))
    rec = _print_record(size=100)
    rec["mid_before"] = None
    rec["mid_after"] = None
    state.recent_prints.append(rec)
    sig = fp.evaluate_footprint(state, cfg, atr=2.0)
    assert sig is None  # mid=None → fail-CLOSED


def test_footprint_zero_size_print_skipped() -> None:
    """Zero-sized print is degenerate — must not fire."""
    state = fp.FootprintAbsorptionState()
    cfg = fp.FootprintAbsorptionConfig(prints_size_z_min=0.0)
    for _ in range(15):
        state.recent_prints.append(_print_record(size=10))
    state.recent_prints.append(_print_record(size=0))
    sig = fp.evaluate_footprint(state, cfg, atr=2.0)
    assert sig is None


# ────────────────────────────────────────────────────────────────────
# footprint_absorption -- B3 cooldown + B6 + I9
# ────────────────────────────────────────────────────────────────────


def test_footprint_B3_cooldown_seconds_blocks_reentry() -> None:
    """Two prints within cooldown_seconds: only the first fires."""
    cfg = fp.FootprintAbsorptionConfig(
        prints_size_z_min=0.5, cooldown_seconds=120.0, absorption_ratio=1.0, absorb_price_band_ticks=10.0
    )
    state = fp.FootprintAbsorptionState()
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    # Build size history with variance so z-score is well-defined.
    # _z_score returns 0 when std==0 (defensive); without variance,
    # no big print can ever clear prints_size_z_min.
    for i, size in enumerate([3, 4, 5, 6, 4, 5, 7, 3, 5, 6, 4, 5, 6, 5, 4]):
        state.recent_prints.append(_print_record(size=size, ts=base + timedelta(seconds=i)))
    # Big print 1 — should fire (size 50 vs mean ~5, std ~1 → z>>0.5)
    state.recent_prints.append(_print_record(size=50, ts=base + timedelta(seconds=20)))
    sig1 = fp.evaluate_footprint(state, cfg, atr=2.0)
    assert sig1 is not None
    # Big print 2 inside cooldown — must NOT fire
    state.recent_prints.append(_print_record(size=60, ts=base + timedelta(seconds=80)))  # only 60s later
    sig2 = fp.evaluate_footprint(state, cfg, atr=2.0)
    assert sig2 is None


def test_footprint_B6_min_stop_ticks_floor_rejects_tiny_atr() -> None:
    """ATR so small the stop collapses below min_stop_ticks * tick →
    refuse to emit (B6 alignment with book_imbalance)."""
    cfg = fp.FootprintAbsorptionConfig(
        prints_size_z_min=0.0, absorption_ratio=1.0, absorb_price_band_ticks=10.0, min_stop_ticks=4, tick_size=0.25
    )
    state = fp.FootprintAbsorptionState()
    # Vary size so std > 0 (z-score is undefined when std=0)
    for s in [3, 4, 5, 6, 4, 5, 7, 3, 5, 6, 4, 5, 6, 5, 4]:
        state.recent_prints.append(_print_record(size=s))
    state.recent_prints.append(_print_record(size=50))
    # ATR = 0.5 → stop_distance = 0.5; min = 4*0.25 = 1.0 → 0.5 < 1.0 → block
    sig = fp.evaluate_footprint(state, cfg, atr=0.5)
    assert sig is None


def test_footprint_B6_signal_id_includes_symbol_side_ts() -> None:
    """signal_id must be deterministic + identifiable."""
    cfg = fp.FootprintAbsorptionConfig(prints_size_z_min=0.0, absorption_ratio=1.0, absorb_price_band_ticks=10.0)
    state = fp.FootprintAbsorptionState()
    # Vary size so std > 0 (z-score is undefined when std=0)
    for s in [3, 4, 5, 6, 4, 5, 7, 3, 5, 6, 4, 5, 6, 5, 4]:
        state.recent_prints.append(_print_record(size=s))
    state.recent_prints.append(_print_record(size=50, side="BUY"))
    sig = fp.evaluate_footprint(state, cfg, atr=2.0, symbol="GC")
    assert sig is not None
    assert "GC" in sig.signal_id
    assert "FOOTPRINT" in sig.signal_id
    assert sig.side in sig.signal_id  # "SHORT" (BUY-print absorbed → SHORT)


def test_footprint_B6_qty_hard_capped_to_one() -> None:
    """Signal must NEVER claim more than 1 contract in shadow mode."""
    cfg = fp.FootprintAbsorptionConfig(prints_size_z_min=0.0, absorption_ratio=1.0, absorb_price_band_ticks=10.0)
    state = fp.FootprintAbsorptionState()
    # Vary size so std > 0 (z-score is undefined when std=0)
    for s in [3, 4, 5, 6, 4, 5, 7, 3, 5, 6, 4, 5, 6, 5, 4]:
        state.recent_prints.append(_print_record(size=s))
    state.recent_prints.append(_print_record(size=50))
    sig = fp.evaluate_footprint(state, cfg, atr=2.0)
    assert sig is not None
    assert sig.qty_contracts == 1


def test_footprint_I9_max_trades_per_day_caps_emits() -> None:
    """After max_trades_per_day fires, no more signals that day."""
    cfg = fp.FootprintAbsorptionConfig(
        prints_size_z_min=0.0,
        absorption_ratio=1.0,
        absorb_price_band_ticks=10.0,
        cooldown_seconds=0.0,
        max_trades_per_day=2,
    )
    state = fp.FootprintAbsorptionState()
    base = datetime.now(UTC).replace(hour=14, minute=0, second=0, microsecond=0)
    # Build small-print history
    for i, s in enumerate([3, 4, 5, 6, 4, 5, 7, 3, 5, 6, 4, 5, 6, 5, 4]):
        state.recent_prints.append(_print_record(size=s, ts=base + timedelta(seconds=i)))

    fires = 0
    for j in range(5):
        state.recent_prints.append(_print_record(size=50, ts=base + timedelta(seconds=20 + j * 5)))
        sig = fp.evaluate_footprint(state, cfg, atr=2.0)
        if sig is not None:
            fires += 1
    assert fires == 2  # capped


# ────────────────────────────────────────────────────────────────────
# aggressor_flow  -- None safety
# ────────────────────────────────────────────────────────────────────


def test_aggressor_none_safety_volume_fields() -> None:
    """volume_buy / volume_sell = None must not crash."""
    bars = [_bar(ts=datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC) + timedelta(minutes=i)) for i in range(5)]
    # Inject None into one bar
    bars[2]["volume_buy"] = None
    bars[3]["volume_sell"] = None
    ratio, sum_b, sum_s = ag.compute_imbalance_ratio(bars)
    # Should compute without crashing; ratio may be small but defined.
    assert isinstance(ratio, float)
    assert sum_b >= 0 and sum_s >= 0


def test_aggressor_none_safety_close_open() -> None:
    """close / open = None must not crash."""
    cfg = ag.AggressorFlowConfig(window_bars=2, consecutive_bars=1, entry_threshold=0.0)
    state = ag.AggressorFlowState()
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    # Build 2 bars with strong buy imbalance
    for i in range(2):
        ag.evaluate_bar(
            _bar(ts=base + timedelta(minutes=i), buy=100, sell=10, close=100.5, open_=100.0), cfg, state, atr=2.0
        )
    # Third bar with close=None — must not crash
    bar_none = _bar(ts=base + timedelta(minutes=2), buy=100, sell=10)
    bar_none["close"] = None
    bar_none["open"] = None
    sig = ag.evaluate_bar(bar_none, cfg, state, atr=2.0)
    # Whatever comes back must not raise.
    assert sig is None or isinstance(sig, ag.AggressorFlowSignal)


# ────────────────────────────────────────────────────────────────────
# aggressor_flow  -- B6 + I9 + cooldown
# ────────────────────────────────────────────────────────────────────


def test_aggressor_B6_min_stop_ticks_floor_rejects_tiny_atr() -> None:
    cfg = ag.AggressorFlowConfig(
        window_bars=2,
        consecutive_bars=1,
        entry_threshold=0.0,
        require_close_confirm=False,
        min_stop_ticks=4,
        tick_size=0.25,
    )
    state = ag.AggressorFlowState()
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    sig = None
    for i in range(3):
        sig = ag.evaluate_bar(
            _bar(ts=base + timedelta(minutes=i), buy=100, sell=10, close=100.5, open_=100.0),
            cfg,
            state,
            atr=0.5,  # too small (0.5 < 4*0.25=1.0)
        )
    assert sig is None


def test_aggressor_B6_signal_id_pattern() -> None:
    cfg = ag.AggressorFlowConfig(window_bars=2, consecutive_bars=1, entry_threshold=0.0, require_close_confirm=False)
    state = ag.AggressorFlowState()
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    signals = []
    for i in range(3):
        s = ag.evaluate_bar(
            _bar(ts=base + timedelta(minutes=i), buy=100, sell=10, close=100.5, open_=100.0),
            cfg,
            state,
            atr=2.0,
            symbol="ES",
        )
        if s is not None:
            signals.append(s)
    assert len(signals) >= 1
    sig = signals[0]
    assert "ES" in sig.signal_id
    assert "AGGFLOW" in sig.signal_id
    assert sig.side in sig.signal_id


def test_aggressor_B6_qty_hard_capped() -> None:
    cfg = ag.AggressorFlowConfig(window_bars=2, consecutive_bars=1, entry_threshold=0.0, require_close_confirm=False)
    state = ag.AggressorFlowState()
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    signals = []
    for i in range(3):
        s = ag.evaluate_bar(
            _bar(ts=base + timedelta(minutes=i), buy=100, sell=10, close=100.5, open_=100.0),
            cfg,
            state,
            atr=2.0,
        )
        if s is not None:
            signals.append(s)
    assert len(signals) >= 1
    assert all(s.qty_contracts == 1 for s in signals)


def test_aggressor_zero_volume_returns_neutral() -> None:
    bars = [
        _bar(ts=datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC) + timedelta(minutes=i), buy=0, sell=0) for i in range(5)
    ]
    ratio, _, _ = ag.compute_imbalance_ratio(bars)
    assert ratio == 0.0


def test_aggressor_cooldown_blocks_immediate_reentry() -> None:
    cfg = ag.AggressorFlowConfig(
        window_bars=2, consecutive_bars=1, entry_threshold=0.0, require_close_confirm=False, cooldown_seconds=300.0
    )
    state = ag.AggressorFlowState()
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    # Bar 0 fills window; bar 1 fires the signal.
    for i in range(2):
        ag.evaluate_bar(
            _bar(ts=base + timedelta(minutes=i), buy=100, sell=10, close=100.5, open_=100.0),
            cfg,
            state,
            atr=2.0,
        )
    assert state.last_signal_dt is not None
    first_sig_dt = state.last_signal_dt
    # Try again 60s later (inside 300s cooldown)
    sig2 = ag.evaluate_bar(
        _bar(ts=base + timedelta(minutes=3), buy=100, sell=10, close=100.5, open_=100.0),
        cfg,
        state,
        atr=2.0,
    )
    assert sig2 is None
    assert state.last_signal_dt == first_sig_dt  # unchanged


# ────────────────────────────────────────────────────────────────────
# microprice_drift -- None safety + B6 + I7 + I8
# ────────────────────────────────────────────────────────────────────


def test_microprice_none_safety_bid_size_field() -> None:
    """bids[0].size = None must not crash → EMPTY_BIDS classification
    (price present, qty absent — treat as one-sided book)."""
    snap = _depth_snap()
    snap["bids"][0]["size"] = None
    micro, mid, cls = mp.compute_microprice(snap)
    assert micro is None
    assert cls == "EMPTY_BIDS"


def test_microprice_none_safety_price_field() -> None:
    """bids[0].price = None must not crash → BOTH_EMPTY."""
    snap = _depth_snap()
    snap["bids"][0]["price"] = None
    micro, mid, cls = mp.compute_microprice(snap)
    assert micro is None


def test_microprice_empty_bids_returns_classification() -> None:
    snap = _depth_snap()
    snap["bids"] = []
    micro, mid, cls = mp.compute_microprice(snap)
    assert micro is None
    assert cls == "EMPTY_BIDS"


def test_microprice_empty_asks_returns_classification() -> None:
    snap = _depth_snap()
    snap["asks"] = []
    micro, mid, cls = mp.compute_microprice(snap)
    assert micro is None
    assert cls == "EMPTY_ASKS"


def test_microprice_I8_zero_qty_fail_closed() -> None:
    """Both sides have qty=0 → BOTH_EMPTY → consecutive counters
    must NOT advance (fail-CLOSED on anomalous book)."""
    cfg = mp.MicropriceConfig(drift_threshold_ticks=0.5, consecutive_snaps=2)
    state = mp.MicropriceState(last_trade_price=99.0)
    state.consecutive_long_count = 1  # primed
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    snap = _depth_snap(bid_qty=0, ask_qty=0, ts=base)
    sig = mp.evaluate_snapshot(snap, cfg, state, atr=2.0)
    assert sig is None
    assert state.consecutive_long_count == 0  # reset


def test_microprice_B6_min_stop_ticks_floor_rejects_tiny_atr() -> None:
    cfg = mp.MicropriceConfig(drift_threshold_ticks=0.5, consecutive_snaps=1, min_stop_ticks=4, tick_size=0.25)
    state = mp.MicropriceState(last_trade_price=99.0)
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    # Big positive drift but atr=0.5 → stop_distance=0.75 < 1.0 floor
    snap = _depth_snap(bid_qty=1, ask_qty=100, ts=base, bid_price=99.75, ask_price=100.25, mid=100.0)
    sig = mp.evaluate_snapshot(snap, cfg, state, atr=0.5)
    assert sig is None


def test_microprice_B6_signal_id_includes_symbol_and_micro() -> None:
    cfg = mp.MicropriceConfig(drift_threshold_ticks=0.5, consecutive_snaps=1)
    state = mp.MicropriceState(last_trade_price=99.0)
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    snap = _depth_snap(bid_qty=1, ask_qty=100, ts=base, bid_price=99.75, ask_price=100.25, mid=100.0)
    sig = mp.evaluate_snapshot(snap, cfg, state, atr=2.0, symbol="GC")
    assert sig is not None
    assert "GC" in sig.signal_id
    assert "MICRO" in sig.signal_id


def test_microprice_I7_gap_aware_reset() -> None:
    """Snapshot arriving > gap_reset_multiple * snapshot_interval
    resets consecutive counters (the increment after reset is the
    new fresh count for this snap, not a continuation of the prior
    run)."""
    cfg = mp.MicropriceConfig(
        drift_threshold_ticks=0.5, consecutive_snaps=3, snapshot_interval_seconds=5.0, gap_reset_multiple=2.0
    )
    state = mp.MicropriceState(last_trade_price=99.0)
    state.consecutive_long_count = 2  # primed to almost-fire
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    state.last_snapshot_dt = base
    # Next snap arrives 30s later (> 5*2 = 10s gap threshold).
    # Drift threshold met, so the post-reset increment puts count
    # back at 1 (not 3 = fire).  Critical assertion: count is BELOW
    # the firing threshold; the strategy did NOT carry over the
    # primed count across the gap.
    snap = _depth_snap(ts=base + timedelta(seconds=30))
    sig = mp.evaluate_snapshot(snap, cfg, state, atr=2.0)
    assert sig is None  # would have fired (3 >= 3) without reset
    assert state.consecutive_long_count < cfg.consecutive_snaps


def test_microprice_consecutive_resets_in_neutral_zone() -> None:
    """Drift within ±threshold neutralizes — counters reset."""
    cfg = mp.MicropriceConfig(drift_threshold_ticks=5.0, consecutive_snaps=3)
    state = mp.MicropriceState(last_trade_price=100.0)
    state.consecutive_long_count = 2
    snap = _depth_snap()  # micro should be very close to 100
    mp.evaluate_snapshot(snap, cfg, state, atr=2.0)
    assert state.consecutive_long_count == 0


# ────────────────────────────────────────────────────────────────────
# Cross-strategy invariants
# ────────────────────────────────────────────────────────────────────


def test_all_three_strategies_default_min_stop_ticks_is_4() -> None:
    """B6 invariant: every L2 strategy enforces min_stop_ticks=4 by
    default.  Changing this requires explicit operator review."""
    assert fp.FootprintAbsorptionConfig().min_stop_ticks == 4
    assert ag.AggressorFlowConfig().min_stop_ticks == 4
    assert mp.MicropriceConfig().min_stop_ticks == 4


def test_all_three_strategies_default_max_trades_per_day_is_6() -> None:
    """I9 invariant: per-strategy daily cap is 6.  Hard cap that
    survives across all 4 strategies."""
    assert fp.FootprintAbsorptionConfig().max_trades_per_day == 6
    assert ag.AggressorFlowConfig().max_trades_per_day == 6
    assert mp.MicropriceConfig().max_trades_per_day == 6


def test_all_three_strategies_emit_qty_contracts_one_in_shadow() -> None:
    """Shadow status implies qty=1 hard cap.  Validated end-to-end
    by exercising each strategy and inspecting the signal."""
    # Footprint
    state_fp = fp.FootprintAbsorptionState()
    cfg_fp = fp.FootprintAbsorptionConfig(prints_size_z_min=0.0, absorption_ratio=1.0, absorb_price_band_ticks=10.0)
    for _ in range(15):
        state_fp.recent_prints.append(_print_record(size=5))
    state_fp.recent_prints.append(_print_record(size=50))
    sig_fp = fp.evaluate_footprint(state_fp, cfg_fp, atr=2.0)
    assert sig_fp is not None and sig_fp.qty_contracts == 1

    # Aggressor
    state_ag = ag.AggressorFlowState()
    cfg_ag = ag.AggressorFlowConfig(window_bars=2, consecutive_bars=1, entry_threshold=0.0, require_close_confirm=False)
    base = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
    ag_signals = []
    for i in range(3):
        s = ag.evaluate_bar(
            _bar(ts=base + timedelta(minutes=i), buy=100, sell=10, close=100.5, open_=100.0),
            cfg_ag,
            state_ag,
            atr=2.0,
        )
        if s is not None:
            ag_signals.append(s)
    assert ag_signals and all(s.qty_contracts == 1 for s in ag_signals)

    # Microprice
    state_mp = mp.MicropriceState(last_trade_price=99.0)
    cfg_mp = mp.MicropriceConfig(drift_threshold_ticks=0.5, consecutive_snaps=1)
    snap = _depth_snap(bid_qty=1, ask_qty=100, bid_price=99.75, ask_price=100.25, mid=100.0)
    sig_mp = mp.evaluate_snapshot(snap, cfg_mp, state_mp, atr=2.0)
    assert sig_mp is not None and sig_mp.qty_contracts == 1


def test_all_three_strategies_factory_exposes_evaluate() -> None:
    """Registry contract: every strategy factory returns an object
    with an .evaluate() method that the supervisor can call."""
    strat_fp = fp.make_footprint_strategy()
    strat_ag = ag.make_aggressor_flow_strategy()
    strat_mp = mp.make_microprice_strategy()
    assert hasattr(strat_fp, "evaluate")
    assert hasattr(strat_ag, "evaluate")
    assert hasattr(strat_mp, "evaluate")
    assert hasattr(strat_fp, "cfg") and hasattr(strat_fp, "state")
    assert hasattr(strat_ag, "cfg") and hasattr(strat_ag, "state")
    assert hasattr(strat_mp, "cfg") and hasattr(strat_mp, "state")


# ────────────────────────────────────────────────────────────────────
# Book-imbalance level-1 None safety (added 2026-05-12)
# ────────────────────────────────────────────────────────────────────


def test_book_imbalance_level_size_none_safe() -> None:
    """A bid or ask level with size=None must classify as EMPTY_<side>
    not crash int()."""
    from eta_engine.strategies import book_imbalance_strategy as bis

    snap = {
        "bids": [{"price": 99.75, "size": None}],
        "asks": [{"price": 100.25, "size": 10}],
        "spread": 0.5,
        "mid": 100.0,
        "ts": datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC).isoformat(),
    }
    ratio, bid_qty, ask_qty, cls = bis._compute_imbalance_with_classification(snap, n_levels=1)
    assert cls == "EMPTY_BIDS"
    assert bid_qty == 0
