"""Tests for scripts.sweep_simulation."""

from __future__ import annotations

from eta_engine.scripts import sweep_simulation as mod


def _std_buckets() -> tuple[mod.Bucket, mod.Bucket, mod.Bucket]:
    mnq = mod.Bucket(
        name="mnq_compounder",
        allocation_pct=0.60,
        trades_per_week=30,
        win_rate=0.59,
        expectancy_r=0.47,
        risk_per_trade_pct=0.01,
    )
    perp = mod.Bucket(
        name="perp_basket",
        allocation_pct=0.30,
        trades_per_week=42,
        win_rate=0.50,
        expectancy_r=0.30,
        risk_per_trade_pct=0.008,
    )
    grid = mod.Bucket(
        name="grid_seed",
        allocation_pct=0.10,
        trades_per_week=40,
        win_rate=0.46,
        expectancy_r=0.15,
        risk_per_trade_pct=0.005,
    )
    return mnq, perp, grid


def test_week_pnl_zero_trades_returns_zero():
    pnl, n = mod._week_pnl(10_000, 0, 0.5, 0.01, 0.5)
    assert pnl == 0.0 and n == 0


def test_week_pnl_zero_equity_returns_zero():
    pnl, n = mod._week_pnl(0.0, 10, 0.5, 0.01, 0.5)
    assert pnl == 0.0 and n == 0


def test_week_pnl_positive_expectancy_yields_positive_pnl():
    pnl, n = mod._week_pnl(10_000, 10, 0.5, 0.01, 0.5)
    # 10 trades * 0.5 exp_r * (10_000 * 0.01) = 500
    assert n == 10
    assert abs(pnl - 500.0) < 1e-9


def test_simulate_grows_equity_with_positive_expectancy():
    mnq, perp, grid = _std_buckets()
    mnq_f, perp_f, grid_f, timeline = mod.simulate(
        27_000.0,
        12,
        mnq,
        perp,
        grid,
        rebalance_every_weeks=4,
    )
    final = mnq_f.equity + perp_f.equity + grid_f.equity
    assert final > 27_000.0
    assert len(timeline) == 12
    # Totals strictly non-decreasing
    totals = [row["total"] for row in timeline]
    for earlier, later in zip(totals, totals[1:], strict=False):
        assert later >= earlier - 1e-6


def test_simulate_respects_zero_weeks():
    mnq, perp, grid = _std_buckets()
    mnq_f, perp_f, grid_f, timeline = mod.simulate(
        10_000.0,
        0,
        mnq,
        perp,
        grid,
    )
    assert timeline == []
    # After 0 weeks buckets were initialized but not traded
    assert abs(mnq_f.equity - 6000.0) < 1e-6
    assert abs(perp_f.equity - 3000.0) < 1e-6
    assert abs(grid_f.equity - 1000.0) < 1e-6


def test_simulate_rebalance_restores_allocation_shares():
    mnq, perp, grid = _std_buckets()
    mod.simulate(27_000.0, 8, mnq, perp, grid, rebalance_every_weeks=4)
    total = mnq.equity + perp.equity + grid.equity
    # After week 8 (multiple of 4), rebalance puts shares at exact 60/30/10
    assert abs(mnq.equity / total - 0.60) < 1e-3
    assert abs(perp.equity / total - 0.30) < 1e-3
    assert abs(grid.equity / total - 0.10) < 1e-3


def test_simulate_with_zero_expectancy_stays_flat():
    mnq = mod.Bucket("m", 0.60, 30, 0.50, 0.0, 0.01)
    perp = mod.Bucket("p", 0.30, 42, 0.50, 0.0, 0.008)
    grid = mod.Bucket("g", 0.10, 40, 0.50, 0.0, 0.005)
    _m, _p, _g, timeline = mod.simulate(10_000.0, 6, mnq, perp, grid)
    totals = [row["total"] for row in timeline]
    for t in totals:
        assert abs(t - 10_000.0) < 1e-6


def test_simulate_negative_expectancy_bleeds_equity():
    mnq = mod.Bucket("m", 0.60, 30, 0.40, -0.10, 0.01)
    perp = mod.Bucket("p", 0.30, 42, 0.40, -0.10, 0.008)
    grid = mod.Bucket("g", 0.10, 40, 0.40, -0.10, 0.005)
    _m, _p, _g, timeline = mod.simulate(10_000.0, 4, mnq, perp, grid, rebalance_every_weeks=0)
    assert timeline[-1]["total"] < 10_000.0


def test_trade_counters_accumulate():
    mnq, perp, grid = _std_buckets()
    mod.simulate(27_000.0, 4, mnq, perp, grid, rebalance_every_weeks=0)
    assert mnq.trades_cum == 30 * 4
    assert perp.trades_cum == 42 * 4
    assert grid.trades_cum == 40 * 4
