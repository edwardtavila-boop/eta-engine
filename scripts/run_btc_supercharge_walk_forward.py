"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_btc_supercharge_walk_forward
=======================================================================
Variant-matrix walk-forward for the BTC +6.00 champion under all
2026-04-27 supercharge upgrades.

Built for the user's "i want everything possible supercharged"
directive. After:

* commit 3cc5fe8 — 5y data extension + +6.00→+1.96 honesty
* commit 7748867 — RegimeGatedStrategy + presets
* commit 7156a4c — regime-gate hypothesis falsified
* (this thread) — engine on_trade_close callback + AdaptiveKelly
                  callback path + 5y BitMEX funding history

This script tests the full variant matrix on 5y BTC 1h:

    1. baseline                    : champion (sage daily + ETF flow)
    2. + funding filter            : extreme_funding_threshold gate
    3. + adaptive Kelly (callback) : trade-level streak amplification
    4. + funding + Kelly           : full stack

Each variant runs a 90/30 walk-forward over 5y data (~57 windows).

Usage::

    python -m eta_engine.scripts.run_btc_supercharge_walk_forward
    python -m eta_engine.scripts.run_btc_supercharge_walk_forward \\
        --variants baseline,kelly,funding,full
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
# Daily sage verdict pre-computation (mirrors prior scripts)
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
        f"daily bars ({daily_bars[0].timestamp.date()} -> "
        f"{daily_bars[-1].timestamp.date()})"
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
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: sage failed at i={i}: {e!r}")
            continue
        bias = r.composite_bias.value
        composite = (
            1.0 if bias == "long" else (-1.0 if bias == "short" else 0.0)
        )
        verdicts[daily_bars[i - 1].timestamp.date()] = SageDailyVerdict(
            direction=bias, conviction=r.conviction, composite=composite,
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
# Strategy factory builder (composable with feature flags)
# ---------------------------------------------------------------------------


def _build_factory(  # noqa: PLR0913
    *,
    provider: Any,  # noqa: ANN401
    etf_path: Path,
    funding_path: Path | None,
    use_funding: bool,
    use_kelly: bool,
) -> Any:  # noqa: ANN401
    """Composable factory: stack the +6.00 champion with optional
    funding filter + adaptive Kelly sizing."""
    from eta_engine.strategies.adaptive_kelly_sizing import (
        AdaptiveKellyConfig,
        AdaptiveKellySizingStrategy,
    )
    from eta_engine.strategies.crypto_macro_confluence_strategy import (
        CryptoMacroConfluenceConfig,
        MacroConfluenceConfig,
    )
    from eta_engine.strategies.crypto_regime_trend_strategy import (
        CryptoRegimeTrendConfig,
    )
    from eta_engine.strategies.macro_confluence_providers import (
        EtfFlowProvider,
        FundingRateProvider,
    )
    from eta_engine.strategies.sage_daily_gated_strategy import (
        SageDailyGatedConfig,
        SageDailyGatedStrategy,
    )

    macro_cfg = MacroConfluenceConfig(
        require_etf_flow_alignment=True,
        # When use_funding is True, gate the entry when |funding| >
        # 0.075% per 8h (~ extreme overheated threshold). A more
        # selective filter than ETF flow.
        extreme_funding_threshold=0.00075 if use_funding else 0.0,
    )
    base_cfg = CryptoMacroConfluenceConfig(
        base=CryptoRegimeTrendConfig(
            regime_ema=100, pullback_ema=21,
            pullback_tolerance_pct=3.0,
            atr_stop_mult=2.0, rr_target=3.0,
        ),
        filters=macro_cfg,
    )

    def _factory():  # noqa: ANN202
        sage_cfg = SageDailyGatedConfig(
            base=base_cfg, min_daily_conviction=0.50, strict_mode=False,
        )
        sage = SageDailyGatedStrategy(sage_cfg)
        sage.attach_etf_flow_provider(EtfFlowProvider(etf_path))
        if use_funding and funding_path is not None and funding_path.exists():
            sage.attach_funding_provider(FundingRateProvider(funding_path))
        if provider is not None:
            sage.attach_daily_verdict_provider(provider)
        if not use_kelly:
            return sage
        # Wrap in AdaptiveKellySizing — engine will auto-attach
        # on_trade_close via WalkForwardEngine duck-typing
        return AdaptiveKellySizingStrategy(
            sage,
            AdaptiveKellyConfig(
                streak_window=5,
                base_multiplier=1.0,
                streak_gain=0.5,
                min_size_multiplier=0.5,
                max_size_multiplier=1.3,
                vol_damping_enabled=True,
            ),
        )

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
    print(f"\n{label}")
    print("=" * 82)
    print(f"Windows:                     {len(res.windows)}")
    print(f"Aggregate OOS Sharpe:        {res.aggregate_oos_sharpe:>8.4f}")
    print(f"Positive OOS windows:        {n_pos}/{len(res.windows)}"
          f" ({n_pos / max(len(res.windows), 1) * 100:.1f}%)")
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
        "--etf-path", type=Path,
        default=Path(r"C:\mnq_data\history\BTC_ETF_FLOWS.csv"),
    )
    parser.add_argument(
        "--funding-path", type=Path,
        default=Path(r"C:\crypto_data\history\BTCFUND_8h.csv"),
    )
    parser.add_argument(
        "--variants", default="baseline,funding,kelly,full",
        help=(
            "Comma-separated variant list. Available: "
            "baseline, funding, kelly, full"
        ),
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
    if not args.funding_path.exists():
        print(f"WARN: funding file missing: {args.funding_path} "
              f"(funding-variant runs will be no-ops)")

    provider = _build_daily_verdicts(args.symbol)

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

    requested = [v.strip() for v in args.variants.split(",") if v.strip()]
    valid_variants = {"baseline", "funding", "kelly", "full"}
    for v in requested:
        if v not in valid_variants:
            print(f"WARN: unknown variant '{v}', skipping")

    variant_specs: dict[str, dict[str, bool]] = {
        "baseline": {"use_funding": False, "use_kelly": False},
        "funding": {"use_funding": True, "use_kelly": False},
        "kelly": {"use_funding": False, "use_kelly": True},
        "full": {"use_funding": True, "use_kelly": True},
    }

    results: dict[str, Any] = {}
    for variant in requested:
        if variant not in variant_specs:
            continue
        spec = variant_specs[variant]
        factory = _build_factory(
            provider=provider,
            etf_path=args.etf_path,
            funding_path=args.funding_path,
            use_funding=spec["use_funding"],
            use_kelly=spec["use_kelly"],
        )
        label = f"variant={variant} (funding={spec['use_funding']} kelly={spec['use_kelly']})"
        res = _run_one(label, factory, bars, backtest_cfg, wf)
        _print_summary(label, res)
        results[variant] = res

    if len(results) >= 2:
        # Side-by-side matrix
        print("\n\nVARIANT MATRIX SUMMARY")
        print("=" * 82)
        cols = ["variant", "OOS Sharpe", "+OOS folds", "deg_avg", "DSR%", "gate"]
        print(f"{cols[0]:<12}{cols[1]:>14}{cols[2]:>14}{cols[3]:>10}"
              f"{cols[4]:>10}{cols[5]:>10}")
        print("-" * 82)
        for variant, res in results.items():
            n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
            n_total = len(res.windows)
            print(
                f"{variant:<12}"
                f"{res.aggregate_oos_sharpe:>14.4f}"
                f"{n_pos:>9}/{n_total:<3}"
                f"{res.oos_degradation_avg:>10.3f}"
                f"{res.fold_dsr_pass_fraction * 100:>9.1f}%"
                f"{('PASS' if res.pass_gate else 'FAIL'):>10}"
            )
        print("=" * 82)

    return 0


if __name__ == "__main__":
    sys.exit(main())
