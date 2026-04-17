"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_walk_forward_demo
================================================
Spin up a 4-window anchored walk-forward run on synthetic bars.
Prints the IS/OOS table + DSR + pass/fail gate.

Usage:
    python -m eta_engine.scripts.run_walk_forward_demo
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _ctx(bar, hist) -> dict:  # noqa: ANN001
    from eta_engine.core.data_pipeline import FundingRate
    now = bar.timestamp
    return {
        "daily_ema": [3000, 3100, 3200, 3300, 3400],
        "h4_struct": "HH_HL", "bias": 1,
        "atr_history": [20] * 10, "atr_current": 20.0,
        "funding_history": [FundingRate(timestamp=now, symbol=bar.symbol,
                                        rate=-0.0008,
                                        predicted_rate=-0.0008)] * 8,
        "onchain": {
            "whale_transfers": 40, "whale_transfers_baseline": 20,
            "exchange_netflow_usd": -30_000_000.0,
            "active_addresses": 1300, "active_addresses_baseline": 1000,
        },
        "sentiment": {
            "galaxy_score": 85.0, "alt_rank": 15, "social_volume": 600,
            "social_volume_baseline": 200, "fear_greed": 20,
        },
    }


def main() -> int:
    from eta_engine.backtest import (
        BacktestConfig,
        BarReplay,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.features.pipeline import FeaturePipeline

    bars = BarReplay.synthetic_bars(
        n=4 * 24 * 30, drift=0.0010, vol=0.004, seed=11,
        start=datetime(2025, 1, 1, tzinfo=UTC), interval_minutes=15,
    )
    cfg = BacktestConfig(
        start_date=bars[0].timestamp, end_date=bars[-1].timestamp,
        symbol=bars[0].symbol, initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=7.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=7, step_days=5, anchored=True,
        oos_fraction=0.3, min_trades_per_window=1,
    )
    res = WalkForwardEngine().run(
        bars=bars, pipeline=FeaturePipeline.default(),
        config=wf, base_backtest_config=cfg, ctx_builder=_ctx,
    )

    print("EVOLUTIONARY TRADING ALGO -- Walk-Forward Demo")
    print("=" * 74)
    print(f"Bars: {len(bars)}  anchored={wf.anchored}  windows={len(res.windows)}")
    print("-" * 74)
    print(f"{'#':>2} {'IS_Sh':>7} {'OOS_Sh':>7} {'IS_tr':>6} {'OOS_tr':>6} "
          f"{'IS_ret%':>8} {'OOS_ret%':>9} {'deg%':>6}")
    print("-" * 74)
    for w in res.windows:
        print(f"{w['window']:>2} {w['is_sharpe']:>7.3f} {w['oos_sharpe']:>7.3f} "
              f"{w['is_trades']:>6} {w['oos_trades']:>6} "
              f"{w['is_return_pct']:>8.2f} {w['oos_return_pct']:>9.2f} "
              f"{w['degradation_pct']*100:>6.1f}")
    print("-" * 74)
    print(f"Aggregate IS Sharpe:       {res.aggregate_is_sharpe:>8.4f}")
    print(f"Aggregate OOS Sharpe:      {res.aggregate_oos_sharpe:>8.4f}")
    print(f"OOS degradation (avg):     {res.oos_degradation_avg*100:>7.2f}%")
    print(f"Deflated Sharpe Ratio:     {res.deflated_sharpe:>8.4f}")
    print(f"Gate (DSR>0.5, deg<35%):   {'PASS' if res.pass_gate else 'FAIL'}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
