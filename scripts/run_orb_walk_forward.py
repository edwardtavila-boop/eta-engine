"""
EVOLUTIONARY TRADING ALGO  //  scripts.run_orb_walk_forward
============================================================
Run the ORB strategy through walk-forward on any symbol the data
library knows about. Replaces / complements
``run_walk_forward_mnq_real`` for ORB-flavored strategies.

Usage::

    # Default: MNQ1 5m, last 6 months, 60-day windows
    python -m eta_engine.scripts.run_orb_walk_forward

    # NQ daily
    MNQ_SYMBOL=NQ1 MNQ_TIMEFRAME=D \\
        python -m eta_engine.scripts.run_orb_walk_forward

    # BTC hourly (after fetch_btc_bars has run)
    MNQ_SYMBOL=BTC MNQ_TIMEFRAME=1h \\
        python -m eta_engine.scripts.run_orb_walk_forward

ORB defaults (range 15m / 11:00 ET cutoff / 9:30 RTH open) are
appropriate for MNQ/NQ. Crypto needs different session times —
override via ORB_RANGE_MINUTES / ORB_TIMEZONE / ORB_RTH_OPEN env
vars (see source for the full list).
"""

from __future__ import annotations

import os
import sys
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _orb_config_from_env():  # type: ignore[no-untyped-def]
    """Read ORB_* env vars. Defaults are MNQ/NQ-tuned."""
    from eta_engine.strategies.orb_strategy import ORBConfig

    def _get_time(key: str, default: time) -> time:
        raw = os.environ.get(key)
        if not raw:
            return default
        h, m = raw.split(":", 1)
        return time(int(h), int(m))

    return ORBConfig(
        range_minutes=int(os.environ.get("ORB_RANGE_MINUTES", "15")),
        rth_open_local=_get_time("ORB_RTH_OPEN", time(9, 30)),
        rth_close_local=_get_time("ORB_RTH_CLOSE", time(16, 0)),
        max_entry_local=_get_time("ORB_MAX_ENTRY", time(11, 0)),
        flatten_at_local=_get_time("ORB_FLATTEN_AT", time(15, 55)),
        timezone_name=os.environ.get("ORB_TIMEZONE", "America/New_York"),
        min_range_pts=float(os.environ.get("ORB_MIN_RANGE", "0.0")),
        ema_bias_period=int(os.environ.get("ORB_EMA_PERIOD", "200")),
        volume_mult=float(os.environ.get("ORB_VOLUME_MULT", "1.0")),
        rr_target=float(os.environ.get("ORB_RR_TARGET", "2.0")),
        atr_period=int(os.environ.get("ORB_ATR_PERIOD", "14")),
        atr_stop_mult=float(os.environ.get("ORB_ATR_STOP_MULT", "2.0")),
        risk_per_trade_pct=float(os.environ.get("ORB_RISK_PCT", "0.01")),
        max_trades_per_day=int(os.environ.get("ORB_MAX_TRADES_PER_DAY", "1")),
    )


def main() -> int:
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.strategies.orb_strategy import ORBStrategy

    symbol = os.environ.get("MNQ_SYMBOL", "MNQ1")
    timeframe = os.environ.get("MNQ_TIMEFRAME", "5m")
    ds = default_library().get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        print(f"ABORT: no dataset for {symbol}/{timeframe} in the data library.")
        return 1
    bars = default_library().load_bars(ds)
    print(
        f"[orb] using {ds.symbol}/{ds.timeframe}/{ds.schema_kind}: "
        f"{ds.row_count} bars over {ds.days_span():.1f} days "
        f"({ds.start_ts.date()} -> {ds.end_ts.date()})"
    )

    cfg = BacktestConfig(
        start_date=bars[0].timestamp, end_date=bars[-1].timestamp,
        symbol=ds.symbol, initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,  # ORB doesn't use confluence
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=int(os.environ.get("WF_WINDOW_DAYS", "60")),
        step_days=int(os.environ.get("WF_STEP_DAYS", "30")),
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=int(os.environ.get("WF_MIN_TRADES", "5")),
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )

    orb_cfg = _orb_config_from_env()

    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=cfg,
        ctx_builder=lambda b, h: {},  # ORB doesn't need ctx
        strategy_factory=lambda: ORBStrategy(orb_cfg),
    )

    print("EVOLUTIONARY TRADING ALGO -- ORB Walk-Forward")
    print("=" * 82)
    print(f"Strategy: ORB(range={orb_cfg.range_minutes}m, "
          f"max_entry={orb_cfg.max_entry_local}, "
          f"rr={orb_cfg.rr_target}, atr_stop={orb_cfg.atr_stop_mult})")
    print(f"Windows: {len(res.windows)}")
    print("-" * 82)
    print(
        f"{'#':>3} {'IS_Sh':>7} {'OOS_Sh':>7} {'IS_tr':>6} {'OOS_tr':>6} "
        f"{'IS_ret%':>8} {'OOS_ret%':>9} {'deg%':>6} {'DSR':>6}"
    )
    print("-" * 82)
    for w in res.windows[-20:]:  # tail only
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
