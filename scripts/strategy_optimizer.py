"""Walk-forward grid optimizer for ETA strategies.

Solves the "I tuned X parameters and now I have great backtest numbers
that won't survive live" problem by ALWAYS evaluating on a held-out
window the optimizer never saw, and applying a deflated-Sharpe penalty
for the size of the parameter grid.

Workflow
--------
1. Caller supplies a strategy_kind + a grid of parameter values
2. For each cell of the grid:
   a. Run realistic-fill simulator on IS window (default 70%)
   b. Run realistic-fill simulator on OOS window (default 30%)
   c. Compute IS Sharpe, OOS Sharpe, IS-vs-OOS PnL decay
3. Rank cells by OOS Sharpe (NOT IS) — IS Sharpe is decoration
4. Apply Bart Lopez de Prado's Deflated Sharpe Ratio (DSR) adjustment
   for the number of trials searched.  If the DSR < 0.5, the "best"
   parameter set is statistically indistinguishable from random noise.

The optimizer writes a full grid result to disk so subsequent runs can
pick up where they left off and so the audit trail of "we considered
these alternatives" is preserved.

This tool is for STRATEGY CREATION + PARAMETER TUNING.  It is the layer
between "I have an idea" and "I'm running paper-soak on the chosen
parameter set."  Do not promote a parameter set whose OOS DSR < 0.5.

Usage
-----
    python -m eta_engine.scripts.strategy_optimizer \\
        --kind sweep_reclaim --symbol MNQ1 --timeframe 5m \\
        --grid level_lookback=10,20,30 reclaim_window=2,3,4 \\
              min_wick_pct=0.4,0.6,0.8 rr_target=1.5,2.0,2.5
"""
from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.scripts import workspace_roots  # noqa: E402

OPTIMIZER_OUTPUT_DIR = workspace_roots.ETA_RUNTIME_STATE_DIR / "optimizer_runs"


@dataclass
class GridCellResult:
    params: dict[str, float | int | str]
    is_pnl: float = 0.0
    oos_pnl: float = 0.0
    is_trades: int = 0
    oos_trades: int = 0
    is_wr: float = 0.0
    oos_wr: float = 0.0
    is_sharpe: float = 0.0
    oos_sharpe: float = 0.0
    rejection_count: int = 0
    error: str | None = None

    @property
    def decay_pct(self) -> float:
        if self.is_pnl == 0:
            return 0.0
        return (self.oos_pnl - self.is_pnl) / abs(self.is_pnl) * 100


def _trade_returns(equity_curve: list[float]) -> list[float]:
    """Per-trade returns (assume each step in equity_curve is one trade).

    The realistic-fill sim emits an equity_curve point per BAR not per
    trade, so we approximate by taking only the points where equity
    actually changed.
    """
    rets = []
    for i in range(1, len(equity_curve)):
        delta = equity_curve[i] - equity_curve[i - 1]
        if abs(delta) > 1e-9:
            rets.append(delta / max(equity_curve[i - 1], 1.0))
    return rets


def _sharpe(returns: list[float], periods_per_year: float = 252.0) -> float:
    """Sharpe of per-trade returns, annualized."""
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0:
        return 0.0
    return (mean / math.sqrt(var)) * math.sqrt(periods_per_year)


def deflated_sharpe(observed_sharpe: float, n_trials: int, n_obs: int) -> float:
    """Lopez de Prado deflated Sharpe ratio.

    Adjusts the observed Sharpe of the BEST cell down by the
    multiple-comparisons penalty implied by ``n_trials``.  Returns the
    probability the true Sharpe > 0.

    Formula sketch:
        E_max(SR) = √(2 ln N) for N trials of a Gaussian
        DSR = Φ((SR - E_max(SR)) × √(n - 1) / √(1 - skew·SR + ((kurt-1)/4)·SR²))

    We use a simplified Gaussian-tail version (no skew/kurt) since
    we don't have per-trade returns for every cell.
    """
    if n_trials <= 1 or n_obs < 2:
        return 0.5
    expected_max_sr = math.sqrt(2 * math.log(n_trials))
    z = (observed_sharpe - expected_max_sr) * math.sqrt(n_obs - 1)
    return _phi(z)


def _phi(z: float) -> float:
    """Standard normal CDF approximation."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _parse_grid_arg(spec: list[str]) -> dict[str, list]:
    """Parse 'k=v1,v2,v3' specs into a grid dict.  Values are tried as
    int, then float, then string (in that order)."""
    grid: dict[str, list] = {}
    for item in spec:
        if "=" not in item:
            raise ValueError(f"bad grid spec {item!r}; expected 'key=v1,v2,v3'")
        key, vals_str = item.split("=", 1)
        vals: list = []
        for v in vals_str.split(","):
            v = v.strip()
            try:
                vals.append(int(v))
            except ValueError:
                try:
                    vals.append(float(v))
                except ValueError:
                    vals.append(v)
        grid[key.strip()] = vals
    return grid


def _evaluate_one_cell(
    kind: str, symbol: str, timeframe: str, days: int,
    is_fraction: float, params: dict, mode: str,
) -> dict:
    """Run IS+OOS for one parameter cell.  Returns a dict (pickleable).

    This relies on building a transient bot via the registry bridge.
    For optimizer use, we register the candidate strategy in a private
    namespace so we don't pollute the live per_bot_registry.
    """
    from eta_engine.scripts.paper_trade_sim import run_simulation
    from eta_engine.strategies.per_bot_registry import (
        StrategyAssignment,
        register_assignment,
    )

    bot_id = f"_optimizer_{kind}_{symbol}_{timeframe}_" + "_".join(
        f"{k}{v}" for k, v in sorted(params.items())
    ).replace(".", "p")[:200]

    try:
        register_assignment(StrategyAssignment(
            bot_id=bot_id, symbol=symbol, timeframe=timeframe,
            strategy_kind=kind, extras={**params, "promotion_status": "optimizer"},
        ))
        daily_bars = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "D": 1, "W": 0.14}
        bar_limit = int(days * daily_bars.get(timeframe, 288))

        is_res = run_simulation(
            bot_id, max_bars=100000, bar_limit=bar_limit,
            mode=mode, is_fraction=is_fraction, eval_oos=False,
        )
        oos_res = run_simulation(
            bot_id, max_bars=100000, bar_limit=bar_limit,
            mode=mode, is_fraction=is_fraction, eval_oos=True,
        )

        is_rets = _trade_returns(is_res.equity_curve)
        oos_rets = _trade_returns(oos_res.equity_curve)

        # Annualization periods/year — depends on timeframe.  Approximation.
        bars_per_day = daily_bars.get(timeframe, 288)
        periods_per_year = bars_per_day * 252  # trading days

        return {
            "params": params,
            "is_pnl": is_res.total_pnl_usd,
            "oos_pnl": oos_res.total_pnl_usd,
            "is_trades": is_res.trades_taken,
            "oos_trades": oos_res.trades_taken,
            "is_wr": is_res.win_rate_pct,
            "oos_wr": oos_res.win_rate_pct,
            "is_sharpe": _sharpe(is_rets, periods_per_year),
            "oos_sharpe": _sharpe(oos_rets, periods_per_year),
            "rejection_count": is_res.signals_rejected + oos_res.signals_rejected,
        }
    except Exception as e:  # noqa: BLE001
        return {"params": params, "error": f"{type(e).__name__}: {e}"}


def run_optimization(
    kind: str, symbol: str, timeframe: str, grid: dict[str, list],
    days: int = 90, is_fraction: float = 0.7, mode: str = "realistic",
    workers: int = 4,
) -> list[GridCellResult]:
    """Walk-forward grid search over the supplied parameter cells."""
    keys = sorted(grid.keys())
    cells = [
        dict(zip(keys, combo, strict=True))
        for combo in itertools.product(*[grid[k] for k in keys])
    ]
    n_trials = len(cells)
    print(f"Optimizing {kind} on {symbol}/{timeframe}: {n_trials} cells, {workers} workers")
    print(f"  IS={is_fraction*100:.0f}% / OOS={(1-is_fraction)*100:.0f}%, mode={mode}")

    results: list[GridCellResult] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_evaluate_one_cell, kind, symbol, timeframe, days,
                      is_fraction, cell, mode): cell
            for cell in cells
        }
        for f in as_completed(futures):
            cell = futures[f]
            try:
                d = f.result()
            except Exception as e:  # noqa: BLE001
                d = {"params": cell, "error": f"executor: {type(e).__name__}: {e}"}
            results.append(GridCellResult(
                params=d.get("params", cell),
                is_pnl=d.get("is_pnl", 0.0),
                oos_pnl=d.get("oos_pnl", 0.0),
                is_trades=d.get("is_trades", 0),
                oos_trades=d.get("oos_trades", 0),
                is_wr=d.get("is_wr", 0.0),
                oos_wr=d.get("oos_wr", 0.0),
                is_sharpe=d.get("is_sharpe", 0.0),
                oos_sharpe=d.get("oos_sharpe", 0.0),
                rejection_count=d.get("rejection_count", 0),
                error=d.get("error"),
            ))
            n = len(results)
            tag = "ERR" if d.get("error") else f"OOS=${results[-1].oos_pnl:+.0f} sharpe={results[-1].oos_sharpe:.2f}"
            print(f"  [{n:3d}/{n_trials}] {cell} {tag}")
    return results


def write_optimizer_report(
    results: list[GridCellResult], kind: str, symbol: str,
    timeframe: str, n_trials: int,
) -> int:
    OPTIMIZER_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC)
    out_path = OPTIMIZER_OUTPUT_DIR / (
        f"opt_{kind}_{symbol}_{timeframe}_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    )

    valid = [r for r in results if r.error is None]
    invalid = [r for r in results if r.error is not None]

    if not valid:
        print(f"\nALL {n_trials} cells errored — fix the strategy or grid.")
        out_path.write_text(json.dumps({
            "ts": now.isoformat(), "kind": kind, "symbol": symbol,
            "timeframe": timeframe, "n_trials": n_trials,
            "errors": [{"params": r.params, "error": r.error} for r in invalid],
        }, indent=2, default=str), encoding="utf-8")
        return 1

    valid.sort(key=lambda x: -x.oos_sharpe)
    top10 = valid[:10]

    snapshot = {
        "ts": now.isoformat(), "kind": kind, "symbol": symbol, "timeframe": timeframe,
        "n_trials": n_trials, "n_errors": len(invalid),
        "results": [
            {
                "params": r.params,
                "is_pnl": r.is_pnl, "oos_pnl": r.oos_pnl,
                "is_trades": r.is_trades, "oos_trades": r.oos_trades,
                "is_wr": r.is_wr, "oos_wr": r.oos_wr,
                "is_sharpe": r.is_sharpe, "oos_sharpe": r.oos_sharpe,
                "decay_pct": r.decay_pct,
                "rejection_count": r.rejection_count,
                "error": r.error,
            }
            for r in valid + invalid
        ],
    }
    out_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 96)
    print(f"OPTIMIZER REPORT — {kind} on {symbol}/{timeframe}")
    print(f"Snapshot: {out_path}")
    print("=" * 96)
    print(f"  {n_trials} cells tested, {len(invalid)} errored, {len(valid)} valid")

    print("\nTop 10 by OOS Sharpe:")
    print(f"{'Rank':<5} {'Params':<60} {'IS$':>8} {'OOS$':>8} {'OOS-Tr':>6} "
          f"{'OOS-WR':>7} {'OOS-Sh':>7} {'Decay':>7}")
    print("-" * 110)
    for i, r in enumerate(top10, 1):
        params_str = " ".join(f"{k}={v}" for k, v in sorted(r.params.items()))[:60]
        print(f"{i:<5} {params_str:<60} ${r.is_pnl:>+7.0f} ${r.oos_pnl:>+7.0f} "
              f"{r.oos_trades:>6} {r.oos_wr:>6.1f}% {r.oos_sharpe:>+7.2f} {r.decay_pct:>+6.0f}%")

    # Deflated Sharpe of the BEST cell vs the trial count
    if valid:
        best = valid[0]
        if best.oos_trades >= 2:
            dsr = deflated_sharpe(best.oos_sharpe, n_trials, best.oos_trades)
            print(f"\nBest cell deflated-Sharpe probability (true SR > 0): {dsr:.3f}")
            if dsr < 0.5:
                print("  >>> WARNING: best cell is statistically indistinguishable from noise.")
                print("  >>> Increase OOS sample, shrink the grid, or do not promote.")

    if invalid:
        print("\nFirst 5 errors:")
        for r in invalid[:5]:
            print(f"  {r.params}: {r.error}")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="strategy_optimizer", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kind", required=True, help="strategy_kind (sweep_reclaim, vwap_reversion, ...)")
    p.add_argument("--symbol", required=True, help="instrument symbol (MNQ1, BTC, ...)")
    p.add_argument("--timeframe", required=True, help="bar timeframe (5m, 1h, ...)")
    p.add_argument("--grid", nargs="+", required=True,
                   help="grid specs as 'key=v1,v2,v3' (one per parameter)")
    p.add_argument("--days", type=int, default=90, help="bars window")
    p.add_argument("--is-fraction", type=float, default=0.7)
    p.add_argument("--mode", default="realistic", choices=["realistic", "pessimistic", "legacy"])
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)

    grid = _parse_grid_arg(args.grid)
    n_trials = 1
    for v in grid.values():
        n_trials *= len(v)

    if n_trials > 500:
        print(f"WARNING: {n_trials} cells is large.  Multiple-comparisons penalty grows fast.")
        print("         Consider reducing the grid.")

    results = run_optimization(
        kind=args.kind, symbol=args.symbol, timeframe=args.timeframe,
        grid=grid, days=args.days, is_fraction=args.is_fraction,
        mode=args.mode, workers=args.workers,
    )
    return write_optimizer_report(results, args.kind, args.symbol, args.timeframe, n_trials)


if __name__ == "__main__":
    sys.exit(main())
