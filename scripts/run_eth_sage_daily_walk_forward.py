"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_eth_sage_daily_walk_forward
=======================================================================
Walk-forward harness for ETH sage-daily-gated crypto_regime_trend / crypto_orb.

The BTC breakthrough (`btc_sage_daily_etf_v1`, agg OOS Sh +6.00,
gate PASS) used this composition:

  * 1h crypto_regime_trend (regime=100, pull=21, atr=2.0, rr=3.0)
  * Farside ETF flow filter (BTC-specific data)
  * Sage's 22-school composite at DAILY cadence as directional veto
    (min_conviction=0.50, loose mode)

This script tests whether the sage-daily-gate alone (without the
ETH-ETF flow data, which we don't have cached) lifts ETH's
crypto_regime_trend baseline or the stronger crypto_orb base. The
hypothesis from the BTC research log is that sage-daily contributed
+1.32 of the +3.04 lift (ETF contributed the other +1.32). If the
hypothesis holds, ETH should see +1-2 OOS Sharpe over plain
regime_trend.

Composition
-----------
``GenericSageDailyGateStrategy(CryptoRegimeTrendStrategy(...))`` or
``GenericSageDailyGateStrategy(crypto_orb_strategy(...))``
+ a daily-sage-verdict provider that pre-computes verdicts on
ETH daily bars once at startup.

Usage::

    python -m eta_engine.scripts.run_eth_sage_daily_walk_forward \\
        [--min-conviction 0.50] [--strict] [--symbol ETH]
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
# Daily sage verdict pre-computation
# ---------------------------------------------------------------------------


def _bar_to_sage_dict(b: Any) -> dict[str, Any]:  # noqa: ANN401 - any BarData
    return {
        "ts": b.timestamp.isoformat(),
        "timestamp": b.timestamp,
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
    }


def _build_daily_verdicts(
    symbol: str,
    instrument_class: str = "crypto",
) -> dict:
    """Pre-compute sage daily verdicts for the symbol's daily bars.

    Returns ``{date: SageDailyVerdict}`` plus a provider callable.
    """
    from eta_engine.brain.jarvis_v3.sage.base import MarketContext
    from eta_engine.brain.jarvis_v3.sage.consultation import consult_sage
    from eta_engine.data.library import default_library
    from eta_engine.strategies.sage_daily_gated_strategy import SageDailyVerdict

    ds = default_library().get(symbol=symbol, timeframe="D")
    if ds is None:
        raise SystemExit(f"ABORT: no daily dataset for {symbol}.")
    daily_bars = default_library().load_bars(ds, require_positive_prices=True)
    if not daily_bars:
        raise SystemExit(f"ABORT: no tradable positive-price daily bars for {symbol}.")
    print(
        f"[sage-daily] consulting sage on {len(daily_bars)} {symbol} "
        f"daily bars ({daily_bars[0].timestamp.date()} -> "
        f"{daily_bars[-1].timestamp.date()})",
        file=sys.stderr,
    )

    verdicts: dict = {}
    sage_dicts = [_bar_to_sage_dict(b) for b in daily_bars]
    # Need 25+ bars for the regime detector; start computation once
    # we've got a meaningful window.
    for i in range(25, len(sage_dicts) + 1):
        ctx = MarketContext(
            bars=sage_dicts[:i][-200:],  # rolling 200-bar context
            side="long",  # arbitrary; we read composite_bias
            entry_price=float(sage_dicts[i - 1]["close"]),
            symbol=symbol,
            instrument_class=instrument_class,
        )
        try:
            r = consult_sage(
                ctx,
                parallel=False,
                use_cache=True,
                apply_edge_weights=False,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: sage failed at i={i}: {e!r}", file=sys.stderr)
            continue
        # Composite -1..+1 (long >0, short <0). Build a Verdict.
        bias = r.composite_bias.value  # 'long'/'short'/'neutral'
        composite = 1.0 if bias == "long" else (-1.0 if bias == "short" else 0.0)
        verdicts[daily_bars[i - 1].timestamp.date()] = SageDailyVerdict(
            direction=bias,
            conviction=r.conviction,
            composite=composite,
        )
    print(f"[sage-daily] computed {len(verdicts)} daily verdicts", file=sys.stderr)

    daily_dates_sorted = sorted(verdicts.keys())

    def _provider(d):  # noqa: ANN001, ANN202
        # Look up most recent verdict at-or-before `d`
        # Binary search would be faster but we have <500 daily bars
        for prev in reversed(daily_dates_sorted):
            if prev <= d:
                return verdicts[prev]
        # No prior verdict → return neutral with conviction 0
        return SageDailyVerdict(direction="neutral", conviction=0.0, composite=0.0)

    return _provider


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ETH")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--min-conviction", type=float, default=0.50)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="Skip sage gate; report plain underlying baseline.",
    )
    parser.add_argument(
        "--base",
        default="regime_trend",
        choices=["regime_trend", "crypto_orb"],
        help=("Underlying strategy. regime_trend = the BTC champion's base; crypto_orb = the ETH sweep winner's base."),
    )
    args = parser.parse_args()

    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.strategies.crypto_regime_trend_strategy import (
        CryptoRegimeTrendConfig,
        CryptoRegimeTrendStrategy,
    )
    from eta_engine.strategies.generic_sage_daily_gate import (
        GenericSageDailyGateConfig,
        GenericSageDailyGateStrategy,
    )

    # 1h LTF dataset
    ds = default_library().get(symbol=args.symbol, timeframe=args.timeframe)
    if ds is None:
        print(f"ABORT: no dataset for {args.symbol}/{args.timeframe}.")
        return 1
    bars = default_library().load_bars(ds, require_positive_prices=True)
    if not bars:
        print(f"ABORT: no tradable positive-price bars for {args.symbol}/{args.timeframe}.")
        return 1
    print(
        f"[wf] {ds.symbol}/{ds.timeframe}: {ds.row_count} bars over "
        f"{ds.days_span():.1f} days "
        f"({ds.start_ts.date()} -> {ds.end_ts.date()})"
    )

    # Pre-compute daily sage verdicts (skip when --baseline-only)
    provider = None if args.baseline_only else _build_daily_verdicts(args.symbol, instrument_class="crypto")

    backtest_cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=ds.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=args.window_days,
        step_days=args.step_days,
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=3,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    base_cfg = CryptoRegimeTrendConfig(
        regime_ema=100,
        pullback_ema=21,
        pullback_tolerance_pct=3.0,
        atr_stop_mult=2.0,
        rr_target=3.0,
    )

    # Plain crypto_orb config — uses the ETH sweep winner's params.
    # Built lazily so the import doesn't fire when not needed.
    def _crypto_orb_factory():  # noqa: ANN202
        from datetime import time

        from eta_engine.strategies.crypto_orb_strategy import (
            CryptoORBConfig,
            crypto_orb_strategy,
        )

        return crypto_orb_strategy(
            CryptoORBConfig(
                range_minutes=120,
                rth_open_local=time(0, 0),
                rth_close_local=time(23, 59),
                max_entry_local=time(6, 0),
                flatten_at_local=time(23, 55),
                timezone_name="UTC",
                atr_stop_mult=3.0,
                rr_target=2.5,
                ema_bias_period=100,
                max_trades_per_day=2,
            )
        )

    def _factory():  # noqa: ANN202
        sub = CryptoRegimeTrendStrategy(base_cfg) if args.base == "regime_trend" else _crypto_orb_factory()
        if provider is None:
            return sub
        wrapped = GenericSageDailyGateStrategy(
            sub,
            GenericSageDailyGateConfig(
                min_daily_conviction=args.min_conviction,
                strict_mode=args.strict,
            ),
        )
        wrapped.attach_daily_verdict_provider(provider)
        return wrapped

    base_label = (
        "crypto_regime_trend(regime=100, pull=21, atr=2.0, rr=3.0)"
        if args.base == "regime_trend"
        else "crypto_orb(range=120m, atr=3.0, rr=2.5, ema=100, max/day=2)"
    )
    label = base_label + (
        f" + sage_daily_gate(conv>={args.min_conviction:.2f}, {'strict' if args.strict else 'loose'})"
        if provider is not None
        else " [BASELINE-ONLY]"
    )
    print(f"[wf] strategy: {label}")

    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=backtest_cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=_factory,
    )

    print(f"\nETH SAGE-DAILY WALK-FORWARD ({datetime.now(UTC).isoformat()})")
    print("=" * 82)
    print(f"Strategy: {label}")
    print(f"Windows: {len(res.windows)}")
    print("-" * 82)
    print(
        f"{'#':>3} {'IS_Sh':>7} {'OOS_Sh':>7} {'IS_tr':>6} {'OOS_tr':>6} "
        f"{'IS_ret%':>8} {'OOS_ret%':>9} {'deg%':>6} {'DSR':>6}"
    )
    print("-" * 82)
    for w in res.windows:
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
    print(f"OOS degradation avg:         {res.oos_degradation_avg:>8.4f}")
    print(f"Per-fold DSR median:         {res.fold_dsr_median:>8.4f}")
    print(f"Per-fold DSR pass fraction:  {res.fold_dsr_pass_fraction * 100:>7.2f}%")
    print(f"Gate: {'PASS' if res.pass_gate else 'FAIL'}")
    print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(main())
