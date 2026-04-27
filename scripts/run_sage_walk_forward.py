"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_sage_walk_forward
=============================================================
Walk-forward harness for the sage-driven strategies:

* ``sage_consensus`` — pure 22-school weighted-vote entry
* ``orb_sage_gated`` — ORB with sage's composite bias as overlay

Runs the same WalkForwardConfig the ORB baseline used (60d window,
30d step, anchored, OOS fraction 0.3) so the metrics line up
cell-for-cell against the existing strategy_baselines.json
entries. Output mirrors ``run_orb_walk_forward.py``.

Usage::

    # MNQ 5m (default)
    python -m eta_engine.scripts.run_sage_walk_forward [--strategy sage_consensus]

    # NQ daily
    MNQ_SYMBOL=NQ1 MNQ_TIMEFRAME=D \\
        python -m eta_engine.scripts.run_sage_walk_forward --strategy sage_consensus

The strategy choice is the only required knob. Sage thresholds are
read from env (SAGE_MIN_CONVICTION etc.) so a sweep script can call
this in a loop.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _sage_cfg_from_env():  # type: ignore[no-untyped-def]  # noqa: ANN202
    from eta_engine.strategies.sage_consensus_strategy import SageConsensusConfig

    return SageConsensusConfig(
        min_conviction=float(os.environ.get("SAGE_MIN_CONVICTION", "0.55")),
        min_consensus=float(os.environ.get("SAGE_MIN_CONSENSUS", "0.30")),
        min_alignment=float(os.environ.get("SAGE_MIN_ALIGNMENT", "0.55")),
        sage_lookback_bars=int(os.environ.get("SAGE_LOOKBACK_BARS", "200")),
        atr_period=int(os.environ.get("SAGE_ATR_PERIOD", "14")),
        atr_stop_mult=float(os.environ.get("SAGE_ATR_STOP_MULT", "1.5")),
        rr_target=float(os.environ.get("SAGE_RR_TARGET", "2.0")),
        risk_per_trade_pct=float(os.environ.get("SAGE_RISK_PCT", "0.01")),
        min_bars_between_trades=int(os.environ.get("SAGE_MIN_BARS_BETWEEN", "6")),
        max_trades_per_day=int(os.environ.get("SAGE_MAX_TRADES_PER_DAY", "3")),
        warmup_bars=int(os.environ.get("SAGE_WARMUP_BARS", "60")),
        instrument_class=os.environ.get("SAGE_INSTRUMENT_CLASS", "futures"),
        apply_edge_weights=False,  # backtests don't have labels yet
    )


def _orb_cfg_from_env():  # type: ignore[no-untyped-def]  # noqa: ANN202
    from datetime import time

    from eta_engine.strategies.orb_strategy import ORBConfig

    return ORBConfig(
        range_minutes=int(os.environ.get("ORB_RANGE_MINUTES", "15")),
        rth_open_local=time(9, 30),
        rth_close_local=time(16, 0),
        max_entry_local=time(11, 0),
        flatten_at_local=time(15, 55),
        timezone_name=os.environ.get("ORB_TIMEZONE", "America/New_York"),
        ema_bias_period=int(os.environ.get("ORB_EMA_PERIOD", "200")),
        rr_target=float(os.environ.get("ORB_RR_TARGET", "2.0")),
        atr_period=int(os.environ.get("ORB_ATR_PERIOD", "14")),
        atr_stop_mult=float(os.environ.get("ORB_ATR_STOP_MULT", "2.0")),
        risk_per_trade_pct=float(os.environ.get("ORB_RISK_PCT", "0.01")),
        max_trades_per_day=int(os.environ.get("ORB_MAX_TRADES_PER_DAY", "1")),
    )


def _build_strategy(name: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
    """Return a (strategy_factory, label) tuple."""
    if name == "sage_consensus":
        from eta_engine.strategies.sage_consensus_strategy import SageConsensusStrategy
        cfg = _sage_cfg_from_env()
        label = (
            f"sage_consensus(conv>={cfg.min_conviction:.2f}, "
            f"align>={cfg.min_alignment:.2f}, lookback={cfg.sage_lookback_bars})"
        )
        return (lambda: SageConsensusStrategy(cfg)), label
    if name == "orb_sage_gated":
        from eta_engine.strategies.sage_gated_orb_strategy import (
            SageGatedORBConfig,
            SageGatedORBStrategy,
        )
        cfg = SageGatedORBConfig(
            orb=_orb_cfg_from_env(),
            sage=_sage_cfg_from_env(),
            overlay_enabled=True,
        )
        label = (
            f"orb_sage_gated(range={cfg.orb.range_minutes}m, "
            f"sage_conv>={cfg.sage.min_conviction:.2f})"
        )
        return (lambda: SageGatedORBStrategy(cfg)), label
    raise ValueError(f"unknown strategy: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategy", default="sage_consensus",
        choices=["sage_consensus", "orb_sage_gated"],
        help="Which sage-driven strategy to run.",
    )
    args = parser.parse_args()

    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline

    symbol = os.environ.get("MNQ_SYMBOL", "MNQ1")
    timeframe = os.environ.get("MNQ_TIMEFRAME", "5m")
    ds = default_library().get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        print(f"ABORT: no dataset for {symbol}/{timeframe} in the data library.")
        return 1
    bars = default_library().load_bars(ds)
    print(
        f"[sage-wf] using {ds.symbol}/{ds.timeframe}/{ds.schema_kind}: "
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
        window_days=int(os.environ.get("WF_WINDOW_DAYS", "60")),
        step_days=int(os.environ.get("WF_STEP_DAYS", "30")),
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

    print("EVOLUTIONARY TRADING ALGO -- Sage Walk-Forward")
    print("=" * 82)
    print(f"Strategy: {label}")
    print(f"Windows: {len(res.windows)}")
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
