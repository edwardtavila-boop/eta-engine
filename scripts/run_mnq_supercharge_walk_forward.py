"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_mnq_supercharge_walk_forward
=======================================================================
Variant-matrix walk-forward for MNQ ORB under the 2026-04-27
supercharge stack.

Why this differs from the BTC version
-------------------------------------
The BTC supercharge thread (commit 973a6aa) found that
RegimeGate + AdaptiveKelly HURT the +6.00 BTC champion. Diagnosis:
the BTC champion's edge was regime-alignment-driven, so post-hoc
filtering / sizing layers were anti-correlated with edge.

MNQ ORB is a FUNDAMENTALLY DIFFERENT mechanic:
* Edge regime: intraday range expansion (ranging tape that breaks)
* Bleeds in: trending tape (range exhausted before breakout)
* Already documented: 2026-04-27 W0 finding showed MNQ ORB
  "+EV in choppy, -EV in trending" — that's EXACTLY the axis
  the regime classifier carves on

So we expect MNQ to RESPOND DIFFERENTLY to the same upgrades.
The ``mnq_intraday_preset()`` RegimeGate is calibrated for 5m
LTF with intraday classifier knobs (EMA 20/60, trend_distance
0.5%, ATR 0.3%) — much tighter than BTC.

Variants
--------
    1. baseline             : plain ORB (mnq_orb_sage_v1's base, no sage)
    2. + regime gate        : mnq_intraday_preset (ranging+mean_revert only)
    3. + adaptive Kelly     : engine callback-driven streak sizing
    4. + regime + Kelly     : full stack

Walk-forward: 60d windows, 30d step (matches mnq_orb_sage_v1 promotion).

Usage
-----
    python -m eta_engine.scripts.run_mnq_supercharge_walk_forward
    python -m eta_engine.scripts.run_mnq_supercharge_walk_forward --variants baseline,regime
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


# ---------------------------------------------------------------------------
# Strategy factory builder (composable with feature flags)
# ---------------------------------------------------------------------------


def _build_factory(  # noqa: C901
    *,
    use_regime_gate: bool,
    use_kelly: bool,
) -> Any:  # noqa: ANN401
    """Composable factory: stack ORB with optional RegimeGate +
    AdaptiveKelly."""
    from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy

    orb_cfg = ORBConfig(
        range_minutes=15,
        rth_open_local=time(9, 30),
        rth_close_local=time(16, 0),
        max_entry_local=time(11, 0),
        flatten_at_local=time(15, 55),
        timezone_name="America/New_York",
        ema_bias_period=200,
        rr_target=2.0,
        atr_period=14,
        atr_stop_mult=2.0,
        risk_per_trade_pct=0.01,
        max_trades_per_day=1,
    )

    def _factory():  # noqa: ANN202
        sub: Any = ORBStrategy(orb_cfg)
        if use_regime_gate:
            from eta_engine.strategies.regime_gated_strategy import (
                RegimeGatedStrategy,
                mnq_intraday_preset,
            )
            sub = RegimeGatedStrategy(sub, mnq_intraday_preset())
        if use_kelly:
            from eta_engine.strategies.adaptive_kelly_sizing import (
                AdaptiveKellyConfig,
                AdaptiveKellySizingStrategy,
            )
            sub = AdaptiveKellySizingStrategy(
                sub,
                AdaptiveKellyConfig(
                    streak_window=5,
                    base_multiplier=1.0,
                    streak_gain=0.5,
                    min_size_multiplier=0.5,
                    max_size_multiplier=1.3,
                    vol_damping_enabled=True,
                ),
            )
        return sub

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
    parser.add_argument("--symbol", default="MNQ1")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--window-days", type=int, default=60)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument(
        "--variants", default="baseline,regime,kelly,full",
        help="Comma-separated: baseline, regime, kelly, full",
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
    variant_specs = {
        "baseline": {"use_regime_gate": False, "use_kelly": False},
        "regime": {"use_regime_gate": True, "use_kelly": False},
        "kelly": {"use_regime_gate": False, "use_kelly": True},
        "full": {"use_regime_gate": True, "use_kelly": True},
    }

    results: dict[str, Any] = {}
    for variant in requested:
        if variant not in variant_specs:
            print(f"WARN: unknown variant '{variant}'")
            continue
        spec = variant_specs[variant]
        factory = _build_factory(
            use_regime_gate=spec["use_regime_gate"],
            use_kelly=spec["use_kelly"],
        )
        label = f"variant={variant} (regime={spec['use_regime_gate']} kelly={spec['use_kelly']})"
        res = _run_one(label, factory, bars, backtest_cfg, wf)
        _print_summary(label, res)
        results[variant] = res

    if len(results) >= 2:
        print("\n\nMNQ VARIANT MATRIX SUMMARY")
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
