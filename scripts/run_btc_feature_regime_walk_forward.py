"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_btc_feature_regime_walk_forward
============================================================================
Walk-forward harness for the +6.00 BTC champion under the
multi-feature regime gate (NEW — replaces failed price-EMA gate).

Why this exists
---------------
The 2026-04-27 supercharge thread proved that the price-EMA + ATR
regime classifier doesn't carve BTC's tape along the same axis as
the +6.00 strategy's edge (commit 7156a4c, deg_avg got worse under
gating).

User insight on the next attempt: with 5y of funding (BitMEX),
ETF flow (Farside), F&G (alternative.me), and sage daily on disk,
classify regime on the FEATURES that ARE correlated with edge,
not on price-derived axes.

This script wraps the +6.00 champion in:
* RegimeGatedStrategy (provider-driven mode)
* attached to a regime-provider built from FeatureRegimeClassifier

Variants:
    1. baseline (ungated)          : control
    2. regime_gate (all features)  : funding + ETF + F&G + sage
    3. regime_gate (sage_only)     : just sage daily verdict
    4. regime_gate (no_funding)    : ETF + F&G + sage only

Usage::

    python -m eta_engine.scripts.run_btc_feature_regime_walk_forward
    python -m eta_engine.scripts.run_btc_feature_regime_walk_forward \\
        --variants baseline,full
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
# Sage verdict pre-compute
# ---------------------------------------------------------------------------


def _bar_to_sage_dict(b: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "ts": b.timestamp.isoformat(),
        "timestamp": b.timestamp,
        "open": float(b.open), "high": float(b.high),
        "low": float(b.low), "close": float(b.close),
        "volume": float(b.volume),
    }


def _build_sage_provider(symbol: str) -> Any:  # noqa: ANN401
    from eta_engine.brain.jarvis_v3.sage.base import MarketContext
    from eta_engine.brain.jarvis_v3.sage.consultation import consult_sage
    from eta_engine.data.library import default_library
    from eta_engine.strategies.sage_daily_gated_strategy import SageDailyVerdict

    ds = default_library().get(symbol=symbol, timeframe="D")
    if ds is None:
        raise SystemExit(f"ABORT: no daily dataset for {symbol}.")
    daily_bars = default_library().load_bars(ds)
    print(f"[sage] consulting on {len(daily_bars)} {symbol} daily bars")
    verdicts: dict = {}
    sd = [_bar_to_sage_dict(b) for b in daily_bars]
    for i in range(25, len(sd) + 1):
        ctx = MarketContext(
            bars=sd[:i][-200:], side="long",
            entry_price=float(sd[i - 1]["close"]),
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
    print(f"[sage] computed {len(verdicts)} verdicts")
    sd_dates = sorted(verdicts.keys())

    def _provider(d):  # noqa: ANN001, ANN202
        for prev in reversed(sd_dates):
            if prev <= d:
                return verdicts[prev]
        return SageDailyVerdict(direction="neutral", conviction=0.0, composite=0.0)

    return _provider


# ---------------------------------------------------------------------------
# Feature-regime provider builder
# ---------------------------------------------------------------------------


def _build_feature_regime_provider(  # noqa: PLR0913
    *, symbol: str, etf_path: Path, funding_path: Path,
    use_funding: bool, use_etf_flow: bool,
    use_fear_greed: bool, use_sage_daily: bool,
    sage_provider: Any | None,  # noqa: ANN401
    bull_threshold: float = 0.30,
    bear_threshold: float = 0.30,
    sage_conviction_floor: float = 0.30,
) -> Any:  # noqa: ANN401
    """Build a regime-provider callable from feature classifications
    on daily BTC bars."""
    from eta_engine.data.library import default_library
    from eta_engine.strategies.feature_regime_classifier import (
        FeatureRegimeClassifier,
        FeatureRegimeConfig,
        make_feature_regime_provider,
    )
    from eta_engine.strategies.macro_confluence_providers import (
        EtfFlowProvider,
        FearGreedProvider,
        FundingRateProvider,
    )

    ds = default_library().get(symbol=symbol, timeframe="D")
    if ds is None:
        raise SystemExit(f"ABORT: no daily dataset for {symbol}.")
    daily_bars = default_library().load_bars(ds)

    cfg = FeatureRegimeConfig(
        use_funding=use_funding,
        use_etf_flow=use_etf_flow,
        use_fear_greed=use_fear_greed,
        use_sage_daily=use_sage_daily,
        funding_extreme=0.0005,
        etf_flow_threshold=200.0,
        fear_greed_extreme=0.6,
        sage_conviction_floor=sage_conviction_floor,
        bull_threshold=bull_threshold,
        bear_threshold=bear_threshold,
    )
    classifier = FeatureRegimeClassifier(cfg)
    if use_funding and funding_path.exists():
        classifier.attach_funding_provider(FundingRateProvider(funding_path))
    if use_etf_flow and etf_path.exists():
        classifier.attach_etf_flow_provider(EtfFlowProvider(etf_path))
    if use_fear_greed:
        # F&G CSV path (canonical location)
        fg_path = Path(r"C:\mnq_data\history\BTC_FEAR_GREED.csv")
        if fg_path.exists():
            classifier.attach_fear_greed_provider(FearGreedProvider(fg_path))
    if use_sage_daily and sage_provider is not None:
        classifier.attach_sage_daily_provider(sage_provider)

    provider = make_feature_regime_provider(classifier, daily_bars)
    print(f"[feature-regime] regime distribution: "
          f"{classifier.regime_distribution}")
    return provider


# ---------------------------------------------------------------------------
# Strategy factory
# ---------------------------------------------------------------------------


def _build_factory(
    *, sage_provider: Any,  # noqa: ANN401
    regime_provider: Any | None,  # noqa: ANN401
    etf_path: Path,
    strict_long_only: bool = False,
) -> Any:  # noqa: ANN401
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
    from eta_engine.strategies.regime_gated_strategy import (
        RegimeGatedStrategy,
        btc_daily_provider_preset,
    )
    from eta_engine.strategies.sage_daily_gated_strategy import (
        SageDailyGatedConfig,
        SageDailyGatedStrategy,
    )

    base_cfg = CryptoMacroConfluenceConfig(
        base=CryptoRegimeTrendConfig(
            regime_ema=100, pullback_ema=21,
            pullback_tolerance_pct=3.0,
            atr_stop_mult=2.0, rr_target=3.0,
        ),
        filters=MacroConfluenceConfig(require_etf_flow_alignment=True),
    )

    def _factory():  # noqa: ANN202
        sage_cfg = SageDailyGatedConfig(
            base=base_cfg, min_daily_conviction=0.50, strict_mode=False,
        )
        sage = SageDailyGatedStrategy(sage_cfg)
        sage.attach_etf_flow_provider(EtfFlowProvider(etf_path))
        sage.attach_daily_verdict_provider(sage_provider)
        if regime_provider is None:
            return sage
        gate_cfg = btc_daily_provider_preset(strict_long_only=strict_long_only)
        wrapped = RegimeGatedStrategy(sage, gate_cfg)
        wrapped.attach_regime_provider(regime_provider)
        return wrapped

    return _factory


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


def _run_one(
    label: str, factory: Any, bars: list, backtest_cfg: Any, wf: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    from eta_engine.backtest import WalkForwardEngine
    from eta_engine.features.pipeline import FeaturePipeline

    print(f"\n[wf] running: {label}")
    return WalkForwardEngine().run(
        bars=bars, pipeline=FeaturePipeline.default(),
        config=wf, base_backtest_config=backtest_cfg,
        ctx_builder=lambda b, h: {}, strategy_factory=factory,
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="BTC")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--window-days", type=int, default=90)
    p.add_argument("--step-days", type=int, default=30)
    p.add_argument(
        "--etf-path", type=Path,
        default=Path(r"C:\mnq_data\history\BTC_ETF_FLOWS.csv"),
    )
    p.add_argument(
        "--funding-path", type=Path,
        default=Path(r"C:\crypto_data\history\BTCFUND_8h.csv"),
    )
    p.add_argument(
        "--variants", default="baseline,full,sage_only,no_funding",
    )
    p.add_argument(
        "--strict-long-only", action="store_true",
        help="Force BUY only under LONG bias (most selective)",
    )
    p.add_argument(
        "--bull-threshold", type=float, default=0.30,
        help="FeatureRegimeConfig.bull_threshold sweep value",
    )
    p.add_argument(
        "--bear-threshold", type=float, default=0.30,
        help="FeatureRegimeConfig.bear_threshold sweep value",
    )
    p.add_argument(
        "--sage-conviction-floor", type=float, default=0.30,
        help="FeatureRegimeConfig.sage_conviction_floor sweep value",
    )
    args = p.parse_args()

    from eta_engine.backtest import BacktestConfig, WalkForwardConfig
    from eta_engine.data.library import default_library

    ds = default_library().get(symbol=args.symbol, timeframe=args.timeframe)
    if ds is None:
        print(f"ABORT: no dataset for {args.symbol}/{args.timeframe}")
        return 1
    bars = default_library().load_bars(ds)
    print(f"[wf] {ds.symbol}/{ds.timeframe}: {ds.row_count} bars over "
          f"{ds.days_span():.1f} days")

    sage_provider = _build_sage_provider(args.symbol)

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

    variant_specs = {
        "baseline": None,
        "full": {"use_funding": True, "use_etf_flow": True,
                 "use_fear_greed": True, "use_sage_daily": True},
        "sage_only": {"use_funding": False, "use_etf_flow": False,
                      "use_fear_greed": False, "use_sage_daily": True},
        "no_funding": {"use_funding": False, "use_etf_flow": True,
                       "use_fear_greed": True, "use_sage_daily": True},
    }
    requested = [v.strip() for v in args.variants.split(",") if v.strip()]
    results: dict[str, Any] = {}
    for variant in requested:
        if variant not in variant_specs:
            print(f"WARN: unknown variant '{variant}'")
            continue
        spec = variant_specs[variant]
        if spec is None:
            regime_provider = None
        else:
            regime_provider = _build_feature_regime_provider(
                symbol=args.symbol, etf_path=args.etf_path,
                funding_path=args.funding_path,
                use_funding=spec["use_funding"],
                use_etf_flow=spec["use_etf_flow"],
                use_fear_greed=spec["use_fear_greed"],
                use_sage_daily=spec["use_sage_daily"],
                sage_provider=sage_provider,
                bull_threshold=args.bull_threshold,
                bear_threshold=args.bear_threshold,
                sage_conviction_floor=args.sage_conviction_floor,
            )
        factory = _build_factory(
            sage_provider=sage_provider,
            regime_provider=regime_provider,
            etf_path=args.etf_path,
            strict_long_only=args.strict_long_only,
        )
        res = _run_one(f"variant={variant}", factory, bars, backtest_cfg, wf)
        _print_summary(f"variant={variant}", res)
        results[variant] = res

    if len(results) >= 2:
        print("\n\nFEATURE-REGIME VARIANT SUMMARY")
        print("=" * 82)
        cols = ["variant", "OOS Sharpe", "+OOS folds", "deg_avg", "DSR%", "gate"]
        print(f"{cols[0]:<14}{cols[1]:>13}{cols[2]:>14}{cols[3]:>10}"
              f"{cols[4]:>10}{cols[5]:>10}")
        print("-" * 82)
        for variant, res in results.items():
            n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
            n_total = len(res.windows)
            print(
                f"{variant:<14}{res.aggregate_oos_sharpe:>13.4f}"
                f"{n_pos:>9}/{n_total:<3}"
                f"{res.oos_degradation_avg:>10.3f}"
                f"{res.fold_dsr_pass_fraction * 100:>9.1f}%"
                f"{('PASS' if res.pass_gate else 'FAIL'):>10}"
            )
        print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(main())
