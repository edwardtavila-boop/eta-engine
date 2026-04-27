"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_btc_regime_gated_walk_forward
========================================================================
Walk-forward harness: BTC +6.00 champion under HTF regime gate.

Built for the 2026-04-27 5-year walk-forward finding (commit
3cc5fe8): the +6.00 BTC OOS was sample-specific. On 5 years of
1h history (43,192 bars) the same strategy averages +1.96 OOS
across 57 windows, with 40% positive folds. The strategy is NOT
curve-fit (deg_avg stayed at 0.238, well below the 0.35 cap) — it
is regime-conditional. It works strongly in some regimes and is
flat-to-negative in others.

This script measures the lift from gating firings on HTF regime
classification using the new ``RegimeGatedStrategy`` wrapper +
``btc_daily_preset()`` (commit 7748867).

Comparison
----------
    Variant 1: SageDailyGatedStrategy(...) — ungated baseline (+1.96)
    Variant 2: RegimeGatedStrategy(SageDailyGatedStrategy(...))
               with btc_daily_preset() — regime-gated

The hypothesis: gating to {trending, ranging} regimes (excluding
volatile drawdown tape) lifts the agg OOS Sharpe back toward
+3-4 territory while keeping deg_avg clean.

Optionally pass --strict-long-only to force BUY-only firings under
LONG bias, which is the strictest setting for a directional bull
strategy like the +6.00 champion.

Usage
-----
    # Default: 5-year walk-forward, 90/30 windows, regime-gated
    python -m eta_engine.scripts.run_btc_regime_gated_walk_forward

    # Compare ungated vs gated side-by-side
    python -m eta_engine.scripts.run_btc_regime_gated_walk_forward --compare

    # Strict long-only mode
    python -m eta_engine.scripts.run_btc_regime_gated_walk_forward \\
        --strict-long-only --compare
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
# Daily sage verdict pre-computation (mirrors run_eth_sage_daily_walk_forward)
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
    """Pre-compute sage daily verdicts for BTC daily bars."""
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
        f"daily bars ({daily_bars[0].timestamp.date()} -> "
        f"{daily_bars[-1].timestamp.date()})"
    )

    verdicts: dict = {}
    sage_dicts = [_bar_to_sage_dict(b) for b in daily_bars]
    for i in range(25, len(sage_dicts) + 1):
        ctx = MarketContext(
            bars=sage_dicts[:i][-200:],
            side="long",
            entry_price=float(sage_dicts[i - 1]["close"]),
            symbol=symbol,
            instrument_class="crypto",
        )
        try:
            r = consult_sage(
                ctx, parallel=False, use_cache=True,
                apply_edge_weights=False,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: sage failed at i={i}: {e!r}")
            continue
        bias = r.composite_bias.value
        composite = (
            1.0 if bias == "long"
            else (-1.0 if bias == "short" else 0.0)
        )
        verdicts[daily_bars[i - 1].timestamp.date()] = SageDailyVerdict(
            direction=bias, conviction=r.conviction, composite=composite,
        )
    print(f"[sage-daily] computed {len(verdicts)} daily verdicts")

    daily_dates_sorted = sorted(verdicts.keys())

    def _provider(d):  # noqa: ANN001, ANN202
        for prev in reversed(daily_dates_sorted):
            if prev <= d:
                return verdicts[prev]
        return SageDailyVerdict(direction="neutral", conviction=0.0, composite=0.0)

    return _provider


# ---------------------------------------------------------------------------
# Daily-bar regime classification pre-computation
# ---------------------------------------------------------------------------


def _build_daily_regime_provider(symbol: str = "BTC") -> Any:  # noqa: ANN401
    """Pre-compute HTF regime classifications on daily BTC bars.

    Returns a provider callable: ``provider(date) -> HtfRegimeClassification``.
    The lookup returns the most recent daily classification at-or-before
    the requested date.

    On daily bars, the default classifier config (50/200 EMA, 220-bar
    warmup, 3% trend-distance, 2% ATR cutoff) is well-calibrated for
    BTC's vol profile. Warmup of 220 daily bars ≈ 7 months — easy on
    1800-day history.
    """
    from eta_engine.data.library import default_library
    from eta_engine.strategies.htf_regime_classifier import (
        HtfRegimeClassification,
        HtfRegimeClassifier,
        HtfRegimeClassifierConfig,
    )

    ds = default_library().get(symbol=symbol, timeframe="D")
    if ds is None:
        raise SystemExit(f"ABORT: no daily dataset for {symbol}.")
    daily_bars = default_library().load_bars(ds)
    print(
        f"[regime] classifying {len(daily_bars)} {symbol} daily bars "
        f"({daily_bars[0].timestamp.date()} -> "
        f"{daily_bars[-1].timestamp.date()})"
    )

    cls_cfg = HtfRegimeClassifierConfig(
        fast_ema=50, slow_ema=200,
        slope_lookback=10, slope_threshold_pct=0.5,
        trend_distance_pct=3.0, range_atr_pct_max=2.0,
        atr_period=14, warmup_bars=220,
    )
    classifier = HtfRegimeClassifier(cls_cfg)

    classifications: dict = {}
    counts = {"trending": 0, "ranging": 0, "volatile": 0}
    for b in daily_bars:
        classifier.update(b)
        cls = classifier.classify(b)
        classifications[b.timestamp.date()] = cls
        counts[cls.regime] = counts.get(cls.regime, 0) + 1
    print(
        f"[regime] regime distribution across {len(daily_bars)} daily bars: "
        f"trending={counts['trending']} "
        f"ranging={counts['ranging']} "
        f"volatile={counts['volatile']}"
    )

    sorted_dates = sorted(classifications.keys())

    def _provider(d):  # noqa: ANN001, ANN202
        for prev in reversed(sorted_dates):
            if prev <= d:
                return classifications[prev]
        # Pre-coverage → return safe-veto neutral/volatile/skip
        return HtfRegimeClassification(
            bias="neutral", regime="volatile", mode="skip",
        )

    return _provider


# ---------------------------------------------------------------------------
# Strategy factories
# ---------------------------------------------------------------------------


def _build_base_factory(provider: Any, etf_path: Path) -> Any:  # noqa: ANN401
    """Factory for the +6.00 champion (ungated baseline)."""
    from eta_engine.strategies.crypto_macro_confluence_strategy import (
        CryptoMacroConfluenceConfig,
        MacroConfluenceConfig,
    )
    from eta_engine.strategies.crypto_regime_trend_strategy import (
        CryptoRegimeTrendConfig,
    )
    from eta_engine.strategies.macro_confluence_providers import (
        EtfFlowProvider,
    )
    from eta_engine.strategies.sage_daily_gated_strategy import (
        SageDailyGatedConfig,
        SageDailyGatedStrategy,
    )

    base_cfg = CryptoMacroConfluenceConfig(
        base=CryptoRegimeTrendConfig(
            regime_ema=100,
            pullback_ema=21,
            pullback_tolerance_pct=3.0,
            atr_stop_mult=2.0,
            rr_target=3.0,
        ),
        filters=MacroConfluenceConfig(require_etf_flow_alignment=True),
    )

    def _factory():  # noqa: ANN202
        sage_cfg = SageDailyGatedConfig(
            base=base_cfg,
            min_daily_conviction=0.50,
            strict_mode=False,
        )
        s = SageDailyGatedStrategy(sage_cfg)
        s.attach_etf_flow_provider(EtfFlowProvider(etf_path))
        if provider is not None:
            s.attach_daily_verdict_provider(provider)
        return s

    return _factory


def _build_regime_gated_factory(
    provider: Any,  # noqa: ANN401
    regime_provider: Any,  # noqa: ANN401
    etf_path: Path,
    *,
    strict_long_only: bool,
) -> Any:  # noqa: ANN401
    """Factory for the regime-gated +6.00 champion.

    Uses ``btc_daily_provider_preset`` + the pre-computed daily-bar
    regime provider. Avoids the LTF-stream-classifier warmup
    problem (4800 bars warmup vs 2160 bars per 90-day window).
    """
    from eta_engine.strategies.regime_gated_strategy import (
        RegimeGatedStrategy,
        btc_daily_provider_preset,
    )

    base_factory = _build_base_factory(provider, etf_path)

    def _factory():  # noqa: ANN202
        sub = base_factory()
        gate_cfg = btc_daily_provider_preset(strict_long_only=strict_long_only)
        wrapped = RegimeGatedStrategy(sub, gate_cfg)
        wrapped.attach_regime_provider(regime_provider)
        return wrapped

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
    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=backtest_cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=factory,
    )
    return res


def _print_summary(label: str, res: Any) -> None:  # noqa: ANN401
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
    print(f"\n{label}")
    print("=" * 82)
    print(f"Windows:                     {len(res.windows)}")
    print(f"Aggregate IS Sharpe:         {res.aggregate_is_sharpe:>8.4f}")
    print(f"Aggregate OOS Sharpe:        {res.aggregate_oos_sharpe:>8.4f}")
    print(f"Positive OOS windows:        {n_pos}/{len(res.windows)}"
          f" ({n_pos / max(len(res.windows), 1) * 100:.1f}%)")
    print(f"OOS degradation avg:         {res.oos_degradation_avg:>8.4f}")
    print(f"Per-fold DSR median:         {res.fold_dsr_median:>8.4f}")
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
        "--etf-path", type=Path,
        default=Path(r"C:\mnq_data\history\BTC_ETF_FLOWS.csv"),
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Run BOTH ungated baseline and regime-gated; compare side-by-side.",
    )
    parser.add_argument(
        "--strict-long-only", action="store_true",
        help="Regime gate forces BUY only under LONG bias.",
    )
    parser.add_argument(
        "--regime-only", action="store_true",
        help="Run only the regime-gated variant (skip baseline).",
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
    if not args.etf_path.exists():
        print(f"ABORT: ETF flow file missing: {args.etf_path}")
        return 1

    provider = _build_daily_verdicts(args.symbol)
    regime_provider = _build_daily_regime_provider(args.symbol)

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

    print(f"\n[wf] window={args.window_days}d step={args.step_days}d  "
          f"strict_long_only={args.strict_long_only}")
    print(f"[wf] timestamp: {datetime.now(UTC).isoformat()}")

    if args.regime_only:
        gated_factory = _build_regime_gated_factory(
            provider, regime_provider, args.etf_path,
            strict_long_only=args.strict_long_only,
        )
        res_g = _run_one(
            "regime-gated +6.00 champion", gated_factory, bars,
            backtest_cfg, wf,
        )
        _print_summary("REGIME-GATED RESULT", res_g)
        return 0

    if args.compare:
        base_factory = _build_base_factory(provider, args.etf_path)
        res_b = _run_one(
            "ungated +6.00 champion (baseline)", base_factory, bars,
            backtest_cfg, wf,
        )
        _print_summary("UNGATED BASELINE", res_b)

        gated_factory = _build_regime_gated_factory(
            provider, regime_provider, args.etf_path,
            strict_long_only=args.strict_long_only,
        )
        res_g = _run_one(
            "regime-gated +6.00 champion", gated_factory, bars,
            backtest_cfg, wf,
        )
        _print_summary("REGIME-GATED RESULT", res_g)

        # Side-by-side comparison
        print("\n\nSIDE-BY-SIDE COMPARISON")
        print("=" * 82)
        print(f"{'metric':<32}{'ungated':>15}{'regime-gated':>15}{'delta':>15}")
        print("-" * 82)
        delta_oos = res_g.aggregate_oos_sharpe - res_b.aggregate_oos_sharpe
        print(
            f"{'aggregate OOS Sharpe':<32}"
            f"{res_b.aggregate_oos_sharpe:>15.4f}"
            f"{res_g.aggregate_oos_sharpe:>15.4f}"
            f"{delta_oos:>+15.4f}"
        )
        n_pos_b = sum(1 for w in res_b.windows if w.get("oos_sharpe", 0) > 0)
        n_pos_g = sum(1 for w in res_g.windows if w.get("oos_sharpe", 0) > 0)
        print(
            f"{'positive OOS windows':<32}"
            f"{n_pos_b:>14}/{len(res_b.windows):<2}"
            f"{n_pos_g:>14}/{len(res_g.windows):<2}"
            f"{n_pos_g - n_pos_b:>+15}"
        )
        print(
            f"{'deg_avg':<32}"
            f"{res_b.oos_degradation_avg:>15.4f}"
            f"{res_g.oos_degradation_avg:>15.4f}"
            f"{res_g.oos_degradation_avg - res_b.oos_degradation_avg:>+15.4f}"
        )
        print(
            f"{'DSR pass fraction':<32}"
            f"{res_b.fold_dsr_pass_fraction * 100:>14.2f}%"
            f"{res_g.fold_dsr_pass_fraction * 100:>14.2f}%"
            f"{(res_g.fold_dsr_pass_fraction - res_b.fold_dsr_pass_fraction) * 100:>+14.2f}%"
        )
        print(
            f"{'gate':<32}"
            f"{'PASS' if res_b.pass_gate else 'FAIL':>15}"
            f"{'PASS' if res_g.pass_gate else 'FAIL':>15}"
        )
        print("=" * 82)
        return 0

    # Default: just run the regime-gated variant
    gated_factory = _build_regime_gated_factory(
        provider, args.etf_path, strict_long_only=args.strict_long_only,
    )
    res_g = _run_one(
        "regime-gated +6.00 champion", gated_factory, bars,
        backtest_cfg, wf,
    )
    _print_summary("REGIME-GATED RESULT", res_g)
    return 0


if __name__ == "__main__":
    sys.exit(main())
