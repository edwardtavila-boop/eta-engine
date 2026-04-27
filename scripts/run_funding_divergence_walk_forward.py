"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_funding_divergence_walk_forward
===========================================================================
Walk-forward harness for FundingDivergenceStrategy.

Built for the 2026-04-27 supercharge follow-on. After the +6.00
champion's regime-conditional edge was confirmed (commit 7156a4c
+ 973a6aa), the path forward shifted to NEW STRATEGIES with
regime-INVARIANT edge.

FundingDivergenceStrategy is the first such attempt. It mean-
reverts on overheated/capitulated derivatives positioning. The
mechanic is uncorrelated with price-EMA structure, so its edge
should not collapse when the price regime turns — the test of
regime-invariance is whether agg OOS Sharpe is stable across
windows that span bull / bear / sideways tape.

Variant sweep
-------------
    1. threshold=+/-0.10% (strict)   : fewer trades, mild extremes
    2. threshold=+/-0.075% (default) : balanced
    3. threshold=+/-0.05% (loose)    : more trades, more noise
    4. threshold=+/-0.075% + sage_confirm : require sage daily alignment

Walk-forward: 90/30 day windows on 5y BTC 1h + 5,475 rows of
8h funding history (BitMEX, May 2021 → Apr 2026).

Usage
-----
    python -m eta_engine.scripts.run_funding_divergence_walk_forward
    python -m eta_engine.scripts.run_funding_divergence_walk_forward \\
        --threshold-sweep 0.0005,0.00075,0.001 --with-sage
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


# ---------------------------------------------------------------------------
# Optional: pre-compute sage daily verdicts
# ---------------------------------------------------------------------------


def _bar_to_sage_dict(b: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "ts": b.timestamp.isoformat(),
        "timestamp": b.timestamp,
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
    }


def _build_daily_verdicts(symbol: str = "BTC") -> Any:  # noqa: ANN401
    from eta_engine.brain.jarvis_v3.sage.base import MarketContext
    from eta_engine.brain.jarvis_v3.sage.consultation import consult_sage
    from eta_engine.data.library import default_library
    from eta_engine.strategies.sage_daily_gated_strategy import SageDailyVerdict

    ds = default_library().get(symbol=symbol, timeframe="D")
    if ds is None:
        raise SystemExit(f"ABORT: no daily dataset for {symbol}.")
    daily_bars = default_library().load_bars(ds)
    print(
        f"[sage-daily] consulting sage on {len(daily_bars)} {symbol} "
        f"daily bars"
    )
    verdicts: dict = {}
    sage_dicts = [_bar_to_sage_dict(b) for b in daily_bars]
    for i in range(25, len(sage_dicts) + 1):
        ctx = MarketContext(
            bars=sage_dicts[:i][-200:], side="long",
            entry_price=float(sage_dicts[i - 1]["close"]),
            symbol=symbol, instrument_class="crypto",
        )
        try:
            r = consult_sage(
                ctx, parallel=False, use_cache=True, apply_edge_weights=False,
            )
        except Exception:  # noqa: BLE001
            continue
        bias = r.composite_bias.value
        verdicts[daily_bars[i - 1].timestamp.date()] = SageDailyVerdict(
            direction=bias, conviction=r.conviction,
            composite=(1.0 if bias == "long" else (-1.0 if bias == "short" else 0.0)),
        )
    print(f"[sage-daily] computed {len(verdicts)} daily verdicts")
    sorted_dates = sorted(verdicts.keys())

    def _provider(d):  # noqa: ANN001, ANN202
        for prev in reversed(sorted_dates):
            if prev <= d:
                return verdicts[prev]
        return SageDailyVerdict(direction="neutral", conviction=0.0, composite=0.0)

    return _provider


# ---------------------------------------------------------------------------
# Strategy factory
# ---------------------------------------------------------------------------


def _build_factory(
    *, threshold: float, funding_path: Path,
    sage_provider: Any = None,  # noqa: ANN401
    require_directional_confirmation: bool = False,
) -> Any:  # noqa: ANN401
    from eta_engine.strategies.funding_divergence_strategy import (
        FundingDivergenceConfig,
        FundingDivergenceStrategy,
    )
    from eta_engine.strategies.macro_confluence_providers import (
        FundingRateProvider,
    )

    cfg = FundingDivergenceConfig(
        entry_threshold=threshold,
        atr_period=14,
        atr_stop_mult=2.0,
        rr_target=2.0,
        risk_per_trade_pct=0.01,
        min_bars_between_trades=24,
        max_trades_per_day=1,
        warmup_bars=50,
        require_directional_confirmation=require_directional_confirmation,
    )

    def _factory():  # noqa: ANN202
        s = FundingDivergenceStrategy(cfg)
        s.attach_funding_provider(FundingRateProvider(funding_path))
        if require_directional_confirmation and sage_provider is not None:
            s.attach_daily_verdict_provider(sage_provider)
        return s

    return _factory


# ---------------------------------------------------------------------------
# Walk-forward execution
# ---------------------------------------------------------------------------


def _run_one(
    label: str, factory: Any, bars: list, backtest_cfg: Any, wf: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    from eta_engine.backtest import WalkForwardEngine
    from eta_engine.features.pipeline import FeaturePipeline

    print(f"\n[wf] running: {label}")
    return WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=backtest_cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=factory,
    )


def _print_summary(label: str, res: Any) -> None:  # noqa: ANN401
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
    n_total = len(res.windows)
    n_oos_trades = sum(w.get("oos_trades", 0) for w in res.windows)
    print(f"\n{label}")
    print("=" * 82)
    print(f"Windows:                     {n_total}")
    print(f"Total OOS trades:            {n_oos_trades}")
    print(f"Aggregate OOS Sharpe:        {res.aggregate_oos_sharpe:>8.4f}")
    print(f"Positive OOS windows:        {n_pos}/{n_total}"
          f" ({n_pos / max(n_total, 1) * 100:.1f}%)")
    print(f"OOS degradation avg:         {res.oos_degradation_avg:>8.4f}")
    print(f"Per-fold DSR pass fraction:  "
          f"{res.fold_dsr_pass_fraction * 100:>7.2f}%")
    print(f"Gate: {'PASS' if res.pass_gate else 'FAIL'}")
    print("=" * 82)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument(
        "--funding-path", type=Path,
        default=Path(r"C:\crypto_data\history\BTCFUND_8h.csv"),
    )
    parser.add_argument(
        "--threshold-sweep", default="0.0005,0.00075,0.001",
        help="Comma-separated entry thresholds (per 8h, decimal).",
    )
    parser.add_argument(
        "--with-sage", action="store_true",
        help="Add a 5th run with require_directional_confirmation=True",
    )
    args = parser.parse_args()

    from eta_engine.backtest import BacktestConfig, WalkForwardConfig
    from eta_engine.data.library import default_library

    ds = default_library().get(symbol=args.symbol, timeframe=args.timeframe)
    if ds is None:
        print(f"ABORT: no dataset for {args.symbol}/{args.timeframe}.")
        return 1
    bars = default_library().load_bars(ds)
    print(
        f"[wf] {ds.symbol}/{ds.timeframe}: {ds.row_count} bars over "
        f"{ds.days_span():.1f} days "
        f"({ds.start_ts.date()} -> {ds.end_ts.date()})"
    )
    if not args.funding_path.exists():
        print(f"ABORT: funding file missing: {args.funding_path}")
        return 1

    backtest_cfg = BacktestConfig(
        start_date=bars[0].timestamp, end_date=bars[-1].timestamp,
        symbol=ds.symbol, initial_equity=10_000.0,
        risk_per_trade_pct=0.01, confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=args.window_days, step_days=args.step_days,
        anchored=True, oos_fraction=0.3,
        min_trades_per_window=3,
        strict_fold_dsr_gate=True, fold_dsr_min_pass_fraction=0.5,
    )

    print(f"\n[wf] window={args.window_days}d step={args.step_days}d")
    print(f"[wf] timestamp: {datetime.now(UTC).isoformat()}")

    thresholds = [float(t.strip()) for t in args.threshold_sweep.split(",") if t.strip()]
    sage_provider = _build_daily_verdicts(args.symbol) if args.with_sage else None

    results: dict[str, Any] = {}
    for thr in thresholds:
        label = f"threshold={thr * 1e4:+.1f}bps (no sage)"
        factory = _build_factory(
            threshold=thr, funding_path=args.funding_path,
            sage_provider=None, require_directional_confirmation=False,
        )
        res = _run_one(label, factory, bars, backtest_cfg, wf)
        _print_summary(label, res)
        results[f"thr={thr}"] = res

    if args.with_sage:
        thr = thresholds[len(thresholds) // 2]  # median
        label = f"threshold={thr * 1e4:+.1f}bps + sage_confirmed"
        factory = _build_factory(
            threshold=thr, funding_path=args.funding_path,
            sage_provider=sage_provider, require_directional_confirmation=True,
        )
        res = _run_one(label, factory, bars, backtest_cfg, wf)
        _print_summary(label, res)
        results[f"thr={thr}+sage"] = res

    if len(results) >= 2:
        print("\n\nFUNDING-DIVERGENCE THRESHOLD SWEEP SUMMARY")
        print("=" * 82)
        cols = ["variant", "OOS Sharpe", "+OOS folds", "OOS trades", "deg_avg", "gate"]
        print(f"{cols[0]:<22}{cols[1]:>13}{cols[2]:>13}"
              f"{cols[3]:>12}{cols[4]:>10}{cols[5]:>10}")
        print("-" * 82)
        for variant, res in results.items():
            n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
            n_total = len(res.windows)
            n_oos = sum(w.get("oos_trades", 0) for w in res.windows)
            print(
                f"{variant:<22}"
                f"{res.aggregate_oos_sharpe:>13.4f}"
                f"{n_pos:>8}/{n_total:<3}"
                f"{n_oos:>12}"
                f"{res.oos_degradation_avg:>10.3f}"
                f"{('PASS' if res.pass_gate else 'FAIL'):>10}"
            )
        print("=" * 82)

    return 0


if __name__ == "__main__":
    sys.exit(main())
