"""Synthetic backtest demo — proves the pipeline runs end-to-end on fake bars.

Uses BarReplay.synthetic_bars() + BacktestEngine + TearsheetBuilder.
Prints a markdown tearsheet to stdout.

Usage:
    python -m eta_engine.scripts.run_backtest_demo
"""

from __future__ import annotations

import sys

from eta_engine.backtest import (
    BacktestConfig,
    BacktestEngine,
    BarReplay,
    TearsheetBuilder,
)
from eta_engine.core.data_pipeline import BarData, FundingRate
from eta_engine.features.pipeline import FeaturePipeline


def _ctx_builder(bar: BarData, hist: list[BarData]) -> dict:
    """Rich context designed to drive confluence above 7.0 in trending regimes."""
    now = bar.timestamp
    # Use recent bars to seed a mock daily EMA series (rising = bull)
    tail = hist[-20:] if len(hist) >= 20 else hist
    ema_series = [b.close for b in tail[:: max(1, len(tail) // 5)]] if len(tail) >= 2 else [bar.close * 0.95, bar.close]
    return {
        "daily_ema": ema_series,
        "h4_struct": "HH_HL",
        "bias": 1,
        "atr_history": [bar.high - bar.low or 1.0] * 10,
        "atr_current": max(bar.high - bar.low, 1.0),
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


def main() -> int:
    bars = BarReplay.synthetic_bars(
        n=400,
        start_price=3500.0,
        drift=0.0008,
        vol=0.008,
        symbol="APEX-SYN",
        seed=42,
    )
    cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol="APEX-SYN",
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=7.0,
        max_trades_per_day=20,
    )
    pipe = FeaturePipeline.default()
    engine = BacktestEngine(pipe, cfg, ctx_builder=_ctx_builder, strategy_id="apex_demo_syn")
    result = engine.run(bars)
    print(TearsheetBuilder.from_result(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
