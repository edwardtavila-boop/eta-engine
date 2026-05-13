"""
EVOLUTIONARY TRADING ALGO  //  scripts.sweep_orb_params
========================================================
Walk-forward parameter sweep for the ORB strategy.

Why
---
The default ORB config produced agg OOS Sharpe +0.80 on MNQ1/5m
with DSR pass fraction at exactly 50% — a hair below the strict
gate's "> 0.5" threshold. The question this script answers: is
there a parameter combination that pushes pass fraction strictly
above 50% AND keeps agg OOS Sharpe positive?

The grid is small on purpose (54 cells with the defaults). Bigger
grids invite p-hacking; we only sweep dimensions where the
strategy review specifically called out tunables:

  * range_minutes — 5 / 15 / 30
  * rr_target     — 1.5 / 2.0 / 3.0
  * atr_stop_mult — 1.0 / 1.5 / 2.0
  * ema_bias_period — 50 / 200

Output is a single sorted markdown table written to
``docs/research_log/orb_sweep_<utc-stamp>.md`` plus stdout.

Usage::

    python -m eta_engine.scripts.sweep_orb_params
        [--symbol MNQ1] [--timeframe 5m]
        [--window-days 60] [--step-days 30]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from eta_engine.scripts import workspace_roots  # noqa: E402


def _parse_int_list(raw: str) -> list[int]:
    values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("at least one integer value is required")
    return values


def _parse_float_list(raw: str) -> list[float]:
    values = [float(part.strip()) for part in raw.split(",") if part.strip()]
    if not values:
        raise argparse.ArgumentTypeError("at least one float value is required")
    return values


def _parse_cells(raw: str) -> list[SweepCell]:
    cells: list[SweepCell] = []
    for chunk in raw.split(","):
        spec = chunk.strip()
        if not spec:
            continue
        parts = [part.strip() for part in spec.split(":")]
        if len(parts) != 4:
            raise argparse.ArgumentTypeError(
                "--cells entries must be range:rr:atr:ema",
            )
        range_minutes, rr_target, atr_stop_mult, ema_bias_period = parts
        cells.append(
            SweepCell(
                int(range_minutes),
                float(rr_target),
                float(atr_stop_mult),
                int(ema_bias_period),
            ),
        )
    if not cells:
        raise argparse.ArgumentTypeError("at least one cell is required")
    return cells


@dataclass(frozen=True)
class SweepCell:
    range_minutes: int
    rr_target: float
    atr_stop_mult: float
    ema_bias_period: int


@dataclass
class SweepResult:
    cell: SweepCell
    n_windows: int
    n_positive_oos: int
    agg_is_sharpe: float
    agg_oos_sharpe: float
    fold_dsr_median: float
    fold_dsr_pass_fraction: float
    pass_gate: bool


def _build_grid(
    *,
    range_minutes: list[int],
    rr_targets: list[float],
    atr_stop_mults: list[float],
    ema_periods: list[int],
) -> list[SweepCell]:
    return [
        SweepCell(rm, rr, asm, ema)
        for rm, rr, asm, ema in product(
            range_minutes,
            rr_targets,
            atr_stop_mults,
            ema_periods,
        )
    ]


def run_one(
    cell: SweepCell,
    *,
    symbol: str,
    timeframe: str,
    window_days: int,
    step_days: int,
    min_trades_per_window: int = 3,
    max_bars: int | None = None,
    bar_slice: str = "tail",
) -> SweepResult:
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy

    ds = default_library().get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        return SweepResult(cell, 0, 0, 0.0, 0.0, 0.0, 0.0, False)
    bars = default_library().load_bars(
        ds,
        limit=max_bars,
        limit_from=bar_slice if max_bars is not None else "head",
        require_positive_prices=True,
    )
    if not bars:
        return SweepResult(cell, 0, 0, 0.0, 0.0, 0.0, 0.0, False)
    cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=ds.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=window_days,
        step_days=step_days,
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=min_trades_per_window,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    orb_cfg = ORBConfig(
        range_minutes=cell.range_minutes,
        rr_target=cell.rr_target,
        atr_stop_mult=cell.atr_stop_mult,
        ema_bias_period=cell.ema_bias_period,
    )
    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=lambda: ORBStrategy(orb_cfg),
    )
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0) > 0)
    return SweepResult(
        cell=cell,
        n_windows=len(res.windows),
        n_positive_oos=n_pos,
        agg_is_sharpe=res.aggregate_is_sharpe,
        agg_oos_sharpe=res.aggregate_oos_sharpe,
        fold_dsr_median=res.fold_dsr_median,
        fold_dsr_pass_fraction=res.fold_dsr_pass_fraction,
        pass_gate=res.pass_gate,
    )


def main() -> int:
    p = argparse.ArgumentParser(prog="sweep_orb_params")
    p.add_argument("--symbol", default="MNQ1")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--window-days", type=int, default=60)
    p.add_argument("--step-days", type=int, default=30)
    p.add_argument("--min-trades-per-window", type=int, default=3)
    p.add_argument(
        "--range-minutes",
        default="5,15,30",
        help="comma-separated ORB range minutes",
    )
    p.add_argument(
        "--rr-targets",
        default="1.5,2.0,3.0",
        help="comma-separated reward/risk targets",
    )
    p.add_argument(
        "--atr-stop-mults",
        default="1.0,1.5,2.0",
        help="comma-separated ATR stop multipliers",
    )
    p.add_argument(
        "--ema-periods",
        default="50,200",
        help="comma-separated EMA bias periods; use 0 to disable EMA bias",
    )
    p.add_argument(
        "--cells",
        default=None,
        help="explicit comma-separated cells as range:rr:atr:ema; overrides grid lists",
    )
    p.add_argument(
        "--max-bars",
        type=int,
        default=None,
        help="cap bars loaded per cell; useful for fast latest-slice tuning",
    )
    p.add_argument(
        "--bar-slice",
        choices=("head", "tail"),
        default="tail",
        help="which side of the dataset to use when --max-bars is set",
    )
    p.add_argument(
        "--report-policy",
        choices=("docs", "runtime"),
        default="docs",
        help="docs = tracked research log; runtime = ignored state report",
    )
    args = p.parse_args()
    if args.min_trades_per_window < 1:
        p.error("--min-trades-per-window must be >= 1")

    grid = (
        _parse_cells(args.cells)
        if args.cells
        else _build_grid(
            range_minutes=_parse_int_list(args.range_minutes),
            rr_targets=_parse_float_list(args.rr_targets),
            atr_stop_mults=_parse_float_list(args.atr_stop_mults),
            ema_periods=_parse_int_list(args.ema_periods),
        )
    )
    print(
        f"[sweep] {args.symbol}/{args.timeframe} — {len(grid)} cells, min_trades/window={args.min_trades_per_window}\n",
    )
    results: list[SweepResult] = []
    for i, cell in enumerate(grid):
        r = run_one(
            cell,
            symbol=args.symbol,
            timeframe=args.timeframe,
            window_days=args.window_days,
            step_days=args.step_days,
            min_trades_per_window=args.min_trades_per_window,
            max_bars=args.max_bars,
            bar_slice=args.bar_slice,
        )
        results.append(r)
        flag = "PASS" if r.pass_gate else ("near" if r.fold_dsr_pass_fraction >= 0.5 else "")
        print(
            f"  {i + 1:2d}/{len(grid)} range={cell.range_minutes:>2}m "
            f"rr={cell.rr_target:>3.1f} atr={cell.atr_stop_mult:>3.1f} "
            f"ema={cell.ema_bias_period:>3d} -> "
            f"OOS={r.agg_oos_sharpe:+.2f} pass={r.fold_dsr_pass_fraction * 100:>4.1f}% {flag}"
        )

    # Sort: passing cells first, then by DSR pass fraction desc, then by OOS Sharpe desc.
    results.sort(
        key=lambda r: (
            not r.pass_gate,
            -r.fold_dsr_pass_fraction,
            -r.agg_oos_sharpe,
        ),
    )

    lines = [
        f"# ORB Parameter Sweep — {args.symbol}/{args.timeframe}",
        "",
        f"_Generated: {datetime.now(UTC).isoformat()}_  "
        f"_Cells: {len(grid)}_  _Windows: {args.window_days}d / step {args.step_days}d_  "
        f"_Min trades/window: {args.min_trades_per_window}_  "
        f"_Bars: {args.bar_slice}:{args.max_bars if args.max_bars is not None else 'all'}_",
        "",
        "| Range | RR | ATR× | EMA | Windows | +OOS | IS Sh | OOS Sh | DSR med | DSR pass% | Verdict |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        verdict = "**PASS**" if r.pass_gate else "FAIL"
        lines.append(
            f"| {r.cell.range_minutes}m | {r.cell.rr_target:.1f} | "
            f"{r.cell.atr_stop_mult:.1f} | {r.cell.ema_bias_period} | "
            f"{r.n_windows} | {r.n_positive_oos} | {r.agg_is_sharpe:+.3f} | "
            f"{r.agg_oos_sharpe:+.3f} | {r.fold_dsr_median:.3f} | "
            f"{r.fold_dsr_pass_fraction * 100:.1f} | {verdict} |"
        )
    md = "\n".join(lines) + "\n"
    print("\n" + md)

    log_dir = (
        ROOT / "docs" / "research_log"
        if args.report_policy == "docs"
        else workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    out_path = log_dir / f"orb_sweep_{args.symbol}_{args.timeframe}_{stamp}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"[saved to {out_path}]")
    n_pass = sum(1 for r in results if r.pass_gate)
    print(f"\nPassing cells: {n_pass}/{len(results)}")
    return 0 if n_pass > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
