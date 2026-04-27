"""
EVOLUTIONARY TRADING ALGO  //  tests.test_backtest
======================================
Covers synthetic bar generation, engine run, metrics math, tearsheet render.
"""

from __future__ import annotations

import pytest

from eta_engine.backtest import (
    BacktestConfig,
    BacktestEngine,
    BarReplay,
    TearsheetBuilder,
    compute_max_dd,
    compute_sharpe,
    compute_sortino,
)
from eta_engine.backtest.models import BacktestResult
from eta_engine.core.data_pipeline import BarData, FundingRate
from eta_engine.features.pipeline import FeaturePipeline


def _win_ctx(bar: BarData, hist: list[BarData]) -> dict:
    now = bar.timestamp
    return {
        "daily_ema": [3000, 3100, 3200, 3300, 3400],
        "h4_struct": "HH_HL",
        "bias": 1,
        "atr_history": [20] * 10,
        "atr_current": 20.0,
        "funding_history": [FundingRate(timestamp=now, symbol=bar.symbol, rate=-0.0008, predicted_rate=-0.0008)] * 8,
        "onchain": {
            "whale_transfers": 40,
            "whale_transfers_baseline": 20,
            "exchange_netflow_usd": -30_000_000.0,
            "active_addresses": 1300,
            "active_addresses_baseline": 1000,
        },
        "sentiment": {
            "galaxy_score": 85.0,
            "alt_rank": 15,
            "social_volume": 600,
            "social_volume_baseline": 200,
            "fear_greed": 20,
        },
    }


def _make_cfg(bars: list[BarData], threshold: float = 7.0) -> BacktestConfig:
    return BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=bars[0].symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=threshold,
        max_trades_per_day=10,
    )


class TestSyntheticBars:
    def test_produces_requested_count(self) -> None:
        assert len(BarReplay.synthetic_bars(n=50, seed=7)) == 50

    def test_bars_are_barData(self) -> None:
        for b in BarReplay.synthetic_bars(n=10, seed=1):
            assert isinstance(b, BarData)
            assert b.high >= max(b.open, b.close) - 1e-9
            assert b.low <= min(b.open, b.close) + 1e-9
            assert b.volume > 0.0

    def test_timestamps_monotonic(self) -> None:
        bars = BarReplay.synthetic_bars(n=20, seed=3, interval_minutes=1)
        for a, b in zip(bars, bars[1:], strict=False):
            assert b.timestamp > a.timestamp

    def test_zero_count_returns_empty(self) -> None:
        assert BarReplay.synthetic_bars(n=0) == []


class TestEngineRun:
    def test_run_produces_result(self) -> None:
        bars = BarReplay.synthetic_bars(n=60, drift=0.001, vol=0.005, seed=11)
        r = BacktestEngine(FeaturePipeline.default(), _make_cfg(bars), ctx_builder=_win_ctx).run(bars)
        assert r.n_trades >= 1
        assert 0.0 <= r.win_rate <= 1.0
        assert r.max_dd_pct >= 0.0

    def test_no_trades_on_empty_ctx(self) -> None:
        bars = BarReplay.synthetic_bars(n=30, seed=5)
        r = BacktestEngine(FeaturePipeline.default(), _make_cfg(bars), ctx_builder=lambda b, h: {}).run(bars)
        assert r.n_trades == 0


class TestMetrics:
    def test_sharpe_positive_with_varied_gains(self) -> None:
        rets = [0.01, 0.008, 0.012, 0.009, 0.011, 0.007, 0.013, 0.01, 0.009, 0.011]
        assert compute_sharpe(rets) > 0

    def test_sharpe_zero_on_zero_stdev(self) -> None:
        assert compute_sharpe([0.01] * 10) == 0.0

    def test_max_dd_on_downward_curve(self) -> None:
        assert compute_max_dd([100, 95, 90, 85, 80]) == pytest.approx(20.0, abs=0.01)

    def test_max_dd_flat_is_zero(self) -> None:
        assert compute_max_dd([100, 100, 100]) == 0.0

    def test_sortino_positive_without_losses(self) -> None:
        assert compute_sortino([0.01, 0.02, 0.03]) > 0.0


class TestTearsheet:
    def test_renders_headline_with_trades(self) -> None:
        bars = BarReplay.synthetic_bars(n=40, drift=0.001, vol=0.005, seed=9)
        r = BacktestEngine(FeaturePipeline.default(), _make_cfg(bars), ctx_builder=_win_ctx).run(bars)
        sheet = TearsheetBuilder.from_result(r)
        assert isinstance(sheet, str)
        assert "Headline Metrics" in sheet
        assert "Win Rate" in sheet
        assert "Total Return" in sheet

    def test_handles_empty_trades(self) -> None:
        r = BacktestResult(
            strategy_id="empty",
            n_trades=0,
            win_rate=0.0,
            avg_win_r=0.0,
            avg_loss_r=0.0,
            expectancy_r=0.0,
            profit_factor=0.0,
            sharpe=0.0,
            sortino=0.0,
            max_dd_pct=0.0,
            total_return_pct=0.0,
            trades=[],
        )
        assert "No trades" in TearsheetBuilder.from_result(r)
