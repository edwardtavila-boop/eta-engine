"""Smoke tests for the public API. Verifies the FP-noise guards,
gate semantics, and drift monitor at the contract level."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_walkforward import (
    BaselineSnapshot,
    Trade,
    WalkForwardConfig,
    WindowStats,
    assess_drift,
    compute_sharpe,
    evaluate_gate,
)


# ---------------------------------------------------------------------------
# compute_sharpe FP-noise + deterministic-R guards
# ---------------------------------------------------------------------------


def test_sharpe_fp_noise_pattern_returns_zero() -> None:
    """3 identical -1pct returns produce sd=1.3e-17 — must NOT blow up."""
    rets = [-0.01, -0.01, -0.010000000000000023]
    assert compute_sharpe(rets) == 0.0


def test_sharpe_deterministic_r_pattern_returns_zero() -> None:
    """4 wins all hitting +1.5R have sd/mean ratio ~1e-5 — also caught."""
    rets = [0.015, 0.015, 0.014999636, 0.014999837]
    assert compute_sharpe(rets) == 0.0


def test_sharpe_real_signal_works() -> None:
    """Honest strategy returns produce a real Sharpe number."""
    rets = [0.01, -0.005, 0.02, 0.005, -0.01, 0.015]
    result = compute_sharpe(rets)
    assert result != 0.0
    assert isinstance(result, float)


def test_sharpe_short_sample_returns_zero() -> None:
    assert compute_sharpe([0.01]) == 0.0
    assert compute_sharpe([]) == 0.0


def test_sharpe_true_zero_stdev_returns_zero() -> None:
    """Constant returns with no FP noise — always 0."""
    assert compute_sharpe([-0.01, -0.01, -0.01]) == 0.0


# ---------------------------------------------------------------------------
# evaluate_gate — strict / long-haul / grid modes
# ---------------------------------------------------------------------------


def _good_window(idx: int) -> WindowStats:
    """A clearly-passing window for fixture purposes."""
    return WindowStats(
        window_index=idx,
        is_sharpe=2.5,
        oos_sharpe=2.0,
        is_trades=20,
        oos_trades=8,
        oos_skew=0.0,
        oos_kurt=3.0,
        oos_profit_factor=1.8,
        oos_max_dd_pct=5.0,
    )


def _is_negative_window(idx: int) -> WindowStats:
    """An IS-negative / OOS-positive window — must NOT pass the gate."""
    return WindowStats(
        window_index=idx,
        is_sharpe=-3.0,
        oos_sharpe=+5.0,
        is_trades=20,
        oos_trades=8,
    )


def test_strict_gate_passes_clean_signal() -> None:
    cfg = WalkForwardConfig(
        window_days=60, step_days=30,
        min_trades_per_window=3,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    windows = [_good_window(i) for i in range(8)]
    result = evaluate_gate(cfg, windows)
    assert result.pass_gate is True
    assert result.aggregate_is_sharpe > 0
    assert result.aggregate_oos_sharpe > 0


def test_strict_gate_rejects_is_negative() -> None:
    """IS-positive gate must catch IS-negative + OOS-positive lucky split."""
    cfg = WalkForwardConfig(
        window_days=60, step_days=30,
        min_trades_per_window=3,
        strict_fold_dsr_gate=True,
    )
    windows = [_is_negative_window(i) for i in range(8)]
    result = evaluate_gate(cfg, windows)
    assert result.pass_gate is False
    assert any("not positive" in r for r in result.reasons)


def test_long_haul_mode_passes_when_pos_fraction_meets_threshold() -> None:
    cfg = WalkForwardConfig(
        window_days=365, step_days=180,
        min_trades_per_window=3,
        long_haul_mode=True,
        long_haul_min_pos_fraction=0.55,
    )
    # 7 of 10 positive → 70% pos_frac, above 55% threshold.
    # Trade counts are large enough for the aggregate DSR to clear 0.5
    # (DSR scales with sqrt(n_trades_total)).
    windows = [
        WindowStats(
            window_index=i,
            is_sharpe=1.5,
            oos_sharpe=2.5 if i < 7 else -0.3,
            is_trades=50,
            oos_trades=20,
        )
        for i in range(10)
    ]
    result = evaluate_gate(cfg, windows)
    assert result.pass_gate is True, f"reasons: {result.reasons}"


def test_long_haul_mode_rejects_low_pos_fraction() -> None:
    cfg = WalkForwardConfig(
        window_days=365, step_days=180,
        min_trades_per_window=3,
        long_haul_mode=True,
        long_haul_min_pos_fraction=0.55,
    )
    # 4 of 10 positive → 40% < 55%
    windows = [
        WindowStats(
            window_index=i,
            is_sharpe=1.0,
            oos_sharpe=2.0 if i < 4 else -1.0,
            is_trades=10, oos_trades=4,
        )
        for i in range(10)
    ]
    result = evaluate_gate(cfg, windows)
    assert result.pass_gate is False


def test_grid_mode_uses_profit_factor_not_sharpe() -> None:
    cfg = WalkForwardConfig(
        window_days=90, step_days=30,
        min_trades_per_window=3,
        grid_mode=True,
        grid_min_profit_factor=1.3,
        grid_max_dd_pct=20.0,
        grid_min_pos_fraction=0.55,
    )
    # Modest Sharpe but solid profit factor → grid_mode passes
    windows = [
        WindowStats(
            window_index=i,
            is_sharpe=0.3,
            oos_sharpe=0.5,
            is_trades=20,
            oos_trades=15,
            oos_profit_factor=1.5,
            oos_max_dd_pct=8.0,
        )
        for i in range(8)
    ]
    result = evaluate_gate(cfg, windows)
    assert result.pass_gate is True


def test_grid_mode_rejects_pf_below_threshold() -> None:
    cfg = WalkForwardConfig(
        window_days=90, step_days=30,
        min_trades_per_window=3,
        grid_mode=True,
        grid_min_profit_factor=1.3,
    )
    windows = [
        WindowStats(
            window_index=i,
            is_sharpe=0.5, oos_sharpe=0.5,
            is_trades=20, oos_trades=15,
            oos_profit_factor=0.9,  # losing
            oos_max_dd_pct=15.0,
        )
        for i in range(8)
    ]
    result = evaluate_gate(cfg, windows)
    assert result.pass_gate is False


def test_evaluate_gate_handles_empty_windows() -> None:
    cfg = WalkForwardConfig()
    result = evaluate_gate(cfg, [])
    assert result.pass_gate is False
    assert "no walk-forward windows" in " ".join(result.reasons)


# ---------------------------------------------------------------------------
# Drift monitor
# ---------------------------------------------------------------------------


def _trade(pnl_r: float, ts_offset: int = 0) -> Trade:
    base = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=ts_offset)
    return Trade(
        entry_time=base,
        exit_time=base + timedelta(minutes=15),
        symbol="MNQ",
        side="BUY",
        qty=1.0,
        entry_price=20000.0,
        exit_price=20000.0 + pnl_r * 50,
        pnl_r=pnl_r,
        pnl_usd=pnl_r * 100,
    )


def test_drift_assess_green_when_recent_matches_baseline() -> None:
    baseline = BaselineSnapshot(
        strategy_id="test_v1",
        n_trades=100,
        win_rate=0.5,
        avg_r=0.4,
        r_stddev=1.2,
    )
    # 25 trades, 50% wins, +0.4R avg → matches baseline
    recent = [_trade(0.4, i) for i in range(13)] + [
        _trade(-0.4, i) for i in range(13, 25)
    ]
    a = assess_drift(
        strategy_id="test_v1", recent=recent, baseline=baseline, min_trades=20,
    )
    assert a.severity == "green"


def test_drift_assess_red_on_collapsed_winrate() -> None:
    baseline = BaselineSnapshot(
        strategy_id="test_v1",
        n_trades=100,
        win_rate=0.5,
        avg_r=0.4,
        r_stddev=1.0,
    )
    # 25 trades all losers → far below baseline
    recent = [_trade(-1.0, i) for i in range(25)]
    a = assess_drift(
        strategy_id="test_v1", recent=recent, baseline=baseline,
        min_trades=20, amber_z=2.0, red_z=3.0,
    )
    assert a.severity == "red"


def test_drift_assess_green_on_insufficient_sample() -> None:
    """Don't false-alarm on tiny samples."""
    baseline = BaselineSnapshot(
        strategy_id="test_v1",
        n_trades=100, win_rate=0.5, avg_r=0.4, r_stddev=1.0,
    )
    recent = [_trade(-1.0, i) for i in range(5)]  # well below min_trades
    a = assess_drift(
        strategy_id="test_v1", recent=recent, baseline=baseline,
        min_trades=20,
    )
    assert a.severity == "green"


def test_baseline_from_trades_round_trips() -> None:
    trades = [_trade(0.5, i) for i in range(10)] + [
        _trade(-0.3, i) for i in range(10, 20)
    ]
    snap = BaselineSnapshot.from_trades("test_v1", trades)
    assert snap.n_trades == 20
    assert 0.45 < snap.win_rate < 0.55
    assert snap.r_stddev > 0
