"""tests.test_walk_forward — anchored vs rolling, DSR hookup."""

from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.backtest import (
    BacktestConfig,
    BarReplay,
    WalkForwardConfig,
    WalkForwardEngine,
)
from eta_engine.core.data_pipeline import BarData, FundingRate
from eta_engine.features.pipeline import FeaturePipeline


def _ctx(bar: BarData, hist: list[BarData]) -> dict:
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


def _cfg(bars: list[BarData]) -> BacktestConfig:
    return BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=bars[0].symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=7.0,
        max_trades_per_day=10,
    )


class TestWindowConstruction:
    def test_rolling_produces_windows(self) -> None:
        bars = BarReplay.synthetic_bars(
            n=4 * 24 * 15,
            drift=0.0005,
            vol=0.004,
            seed=4,
            start=datetime(2025, 1, 1, tzinfo=UTC),
            interval_minutes=15,
        )  # ~15 days of 15m bars
        eng = WalkForwardEngine()
        wins = eng._build_windows(
            bars,
            WalkForwardConfig(
                window_days=5,
                step_days=2,
                anchored=False,
                oos_fraction=0.4,
            ),
        )
        assert len(wins) >= 2
        # rolling: IS starts move forward
        assert wins[1][0] > wins[0][0]

    def test_anchored_keeps_is_start_fixed(self) -> None:
        bars = BarReplay.synthetic_bars(
            n=4 * 24 * 15,
            drift=0.0005,
            vol=0.004,
            seed=5,
            start=datetime(2025, 1, 1, tzinfo=UTC),
            interval_minutes=15,
        )
        eng = WalkForwardEngine()
        wins = eng._build_windows(
            bars,
            WalkForwardConfig(
                window_days=5,
                step_days=2,
                anchored=True,
                oos_fraction=0.4,
            ),
        )
        if len(wins) >= 2:
            assert wins[0][0] == wins[1][0]


class TestEndToEnd:
    def test_run_yields_result(self) -> None:
        bars = BarReplay.synthetic_bars(
            n=4 * 24 * 20,
            drift=0.0015,
            vol=0.004,
            seed=7,
            start=datetime(2025, 1, 1, tzinfo=UTC),
            interval_minutes=15,
        )
        eng = WalkForwardEngine()
        res = eng.run(
            bars=bars,
            pipeline=FeaturePipeline.default(),
            config=WalkForwardConfig(
                window_days=5,
                step_days=3,
                anchored=False,
                oos_fraction=0.3,
                min_trades_per_window=1,
            ),
            base_backtest_config=_cfg(bars),
            ctx_builder=_ctx,
        )
        assert len(res.windows) >= 1
        assert isinstance(res.deflated_sharpe, float)
        assert isinstance(res.pass_gate, bool)

    def test_empty_bars_returns_empty(self) -> None:
        eng = WalkForwardEngine()
        res = eng.run(
            bars=[],
            pipeline=FeaturePipeline.default(),
            config=WalkForwardConfig(window_days=5, step_days=2),
            base_backtest_config=BacktestConfig(
                start_date=datetime(2025, 1, 1, tzinfo=UTC),
                end_date=datetime(2025, 1, 2, tzinfo=UTC),
                symbol="T",
                initial_equity=10_000.0,
                risk_per_trade_pct=0.01,
            ),
        )
        assert res.windows == []
