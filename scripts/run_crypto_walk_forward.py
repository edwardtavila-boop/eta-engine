"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_crypto_walk_forward
================================================================
Walk-forward harness for the crypto strategy family
(crypto_trend / crypto_meanrev / crypto_scalp / crypto_orb).

The crypto strategy modules shipped 2026-04-27 with unit tests
but were never run through the strict walk-forward gate. This
harness fills that gap, picking the right symbol/timeframe per
strategy and emitting the same metrics the ORB sweep used.

Usage::

    python -m eta_engine.scripts.run_crypto_walk_forward \\
        --strategy crypto_trend [--symbol BTC --timeframe 1h]

Defaults are tuned per strategy:
  * crypto_trend     -> BTC / 1h
  * crypto_meanrev   -> BTC / 1h
  * crypto_scalp     -> BTC / 5m
  * crypto_orb       -> BTC / 1h
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


_DEFAULT_TF: dict[str, str] = {
    "crypto_trend": "1h",
    "crypto_meanrev": "1h",
    "crypto_scalp": "5m",
    "crypto_orb": "1h",
}


def _build_strategy(name: str):  # noqa: ANN202 - factory closure
    """Return (strategy_factory, label) for the chosen name."""
    if name == "crypto_trend":
        from eta_engine.strategies.crypto_trend_strategy import (
            CryptoTrendConfig,
            CryptoTrendStrategy,
        )
        cfg = CryptoTrendConfig()
        return (lambda: CryptoTrendStrategy(cfg)), (
            f"crypto_trend(fast={cfg.fast_ema}, slow={cfg.slow_ema}, "
            f"htf={cfg.htf_ema}, atr_stop={cfg.atr_stop_mult}, "
            f"rr={cfg.rr_target})"
        )
    if name == "crypto_meanrev":
        from eta_engine.strategies.crypto_meanrev_strategy import (
            CryptoMeanRevConfig,
            CryptoMeanRevStrategy,
        )
        cfg = CryptoMeanRevConfig()
        return (lambda: CryptoMeanRevStrategy(cfg)), (
            f"crypto_meanrev(bb={cfg.bb_period}/{cfg.bb_stddev_mult}, "
            f"rsi={cfg.rsi_period} {cfg.rsi_oversold}/{cfg.rsi_overbought}, "
            f"rr={cfg.rr_target})"
        )
    if name == "crypto_scalp":
        from eta_engine.strategies.crypto_scalp_strategy import (
            CryptoScalpConfig,
            CryptoScalpStrategy,
        )
        cfg = CryptoScalpConfig()
        return (lambda: CryptoScalpStrategy(cfg)), (
            f"crypto_scalp(lookback={cfg.lookback_bars}, "
            f"vwap={cfg.require_vwap_alignment}, rsi>={cfg.rsi_long_min}, "
            f"atr_stop={cfg.atr_stop_mult})"
        )
    if name == "crypto_orb":
        from eta_engine.strategies.crypto_orb_strategy import (
            CryptoORBConfig,
            crypto_orb_strategy,
        )
        cfg = CryptoORBConfig()
        return (lambda: crypto_orb_strategy(cfg)), (
            f"crypto_orb(range={cfg.range_minutes}m, "
            f"atr_stop={cfg.atr_stop_mult}, rr={cfg.rr_target})"
        )
    raise ValueError(f"unknown strategy: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategy", required=True,
        choices=list(_DEFAULT_TF.keys()),
        help="Crypto strategy module to walk-forward.",
    )
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--step-days", type=int, default=30)
    args = parser.parse_args()

    timeframe = args.timeframe or _DEFAULT_TF[args.strategy]

    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline

    ds = default_library().get(symbol=args.symbol, timeframe=timeframe)
    if ds is None:
        print(f"ABORT: no dataset for {args.symbol}/{timeframe}.")
        return 1
    bars = default_library().load_bars(ds)
    print(
        f"[crypto-wf] using {ds.symbol}/{ds.timeframe}/{ds.schema_kind}: "
        f"{ds.row_count} bars over {ds.days_span():.1f} days "
        f"({ds.start_ts.date()} -> {ds.end_ts.date()})"
    )

    cfg = BacktestConfig(
        start_date=bars[0].timestamp, end_date=bars[-1].timestamp,
        symbol=ds.symbol, initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=args.window_days,
        step_days=args.step_days,
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=int(os.environ.get("WF_MIN_TRADES", "3")),
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )

    factory, label = _build_strategy(args.strategy)

    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=factory,
    )

    print("EVOLUTIONARY TRADING ALGO -- Crypto Walk-Forward")
    print("=" * 82)
    print(f"Strategy: {label}")
    print(f"Symbol/TF: {args.symbol}/{timeframe}  Windows: {len(res.windows)}")
    print("-" * 82)
    print(
        f"{'#':>3} {'IS_Sh':>7} {'OOS_Sh':>7} {'IS_tr':>6} {'OOS_tr':>6} "
        f"{'IS_ret%':>8} {'OOS_ret%':>9} {'deg%':>6} {'DSR':>6}"
    )
    print("-" * 82)
    for w in res.windows[-20:]:
        print(
            f"{w['window']:>3} {w['is_sharpe']:>7.3f} {w['oos_sharpe']:>7.3f} "
            f"{w['is_trades']:>6} {w['oos_trades']:>6} "
            f"{w['is_return_pct']:>8.2f} {w['oos_return_pct']:>9.2f} "
            f"{w['degradation_pct'] * 100:>6.1f} {w.get('oos_dsr', 0.0):>6.3f}"
        )
    print("-" * 82)
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
    print(f"Aggregate IS Sharpe:         {res.aggregate_is_sharpe:>8.4f}")
    print(f"Aggregate OOS Sharpe:        {res.aggregate_oos_sharpe:>8.4f}")
    print(f"Positive OOS windows:        {n_pos}/{len(res.windows)}")
    print(f"Per-fold DSR median:         {res.fold_dsr_median:>8.4f}")
    print(f"Per-fold DSR pass fraction:  {res.fold_dsr_pass_fraction * 100:>7.2f}%")
    print(f"Gate: {'PASS' if res.pass_gate else 'FAIL'}")
    print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(main())
