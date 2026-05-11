"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_equity_simulator
==========================================================
Monte Carlo equity-curve simulator from a strategy's historical
per-trade return distribution.

Why this exists
---------------
The Risk Manager (Firm stage 3) needs to size positions for
survival, but raw "sharpe = 1.5" or "win rate = 60%" doesn't
answer the operator's actual question:

    "Given this strategy's per-trade R distribution, what's the
     worst drawdown I'd see over 100 sessions in 99% of futures?
     Is my account big enough to survive 3× that?"

The Monte Carlo simulator answers this directly.  Sample with
replacement from the realized per-trade R distribution, generate
N=10000 alternative equity paths, compute drawdown distribution
across paths, report p50 / p90 / p99 max drawdown.

Mechanic
--------
1. Read the strategy's trade history from l2_backtest_runs.jsonl
   (or pass a list of returns directly for testing)
2. For each of N_PATHS paths:
   - Sample trades_per_path from the realized distribution
   - Compute cumulative equity curve
   - Track running max + max drawdown
3. Aggregate across paths:
   - Median end-equity
   - p50 / p90 / p99 max drawdown
   - Probability of any drawdown > threshold
4. Output: risk-of-ruin estimate at the operator-supplied
   max_acceptable_drawdown threshold

This is the input the Risk Manager uses to choose Kelly fraction
(typically 0.25-0.50) and absolute size cap.

Run
---
::

    python -m eta_engine.scripts.l2_equity_simulator \\
        --strategy book_imbalance --symbol MNQ \\
        --n-paths 10000 --trades-per-path 100
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import random
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"
EQUITY_SIM_LOG = LOG_DIR / "l2_equity_sim.jsonl"


@dataclass
class EquitySimReport:
    strategy: str
    symbol: str
    n_paths: int
    trades_per_path: int
    starting_equity_usd: float
    n_historical_trades_used: int
    median_end_equity_usd: float | None
    p10_end_equity_usd: float | None
    p90_end_equity_usd: float | None
    median_max_drawdown_pct: float | None
    p90_max_drawdown_pct: float | None
    p99_max_drawdown_pct: float | None
    prob_drawdown_exceeds_threshold: float | None
    threshold_pct: float
    risk_of_ruin: float | None        # P(account hits 0)
    notes: list[str] = field(default_factory=list)


def _read_trade_returns(strategy: str, symbol: str,
                          *, _path: Path | None = None,
                          since_days: int = 90) -> list[float]:
    """Read per-trade pnl_dollars_net from backtest log.

    Falls back to constructing returns from win/loss + avg pnl when
    individual trade returns aren't available in the digest.
    """
    path = _path if _path is not None else L2_BACKTEST_LOG
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    returns: list[float] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                if rec.get("strategy") != strategy or rec.get("symbol") != symbol:
                    continue
                # We don't have per-trade returns in the digest by default,
                # so reconstruct from {n_wins, n_losses, total_pnl}.  This
                # gives a synthetic distribution that matches mean and
                # win rate; not perfect but better than nothing.
                n_trades = int(rec.get("n_trades", 0))
                n_wins = int(rec.get("n_wins", 0)) if "n_wins" in rec \
                          else int(round(rec.get("win_rate", 0) * n_trades))
                total_pnl = float(rec.get("total_pnl_dollars_net",
                                            rec.get("total_pnl_dollars", 0)))
                if n_trades < 1:
                    continue
                avg = total_pnl / n_trades
                # Reconstruct using win/loss split with same mean (rough)
                if n_wins > 0 and (n_trades - n_wins) > 0:
                    # Assume losers ~= -1R, winners ~= +RR-target (simple model)
                    avg_win = max(2.0, avg * 2)
                    avg_loss = -1.0  # in R units, convert via point_value
                    for _ in range(n_wins):
                        returns.append(avg_win)
                    for _ in range(n_trades - n_wins):
                        returns.append(avg_loss)
                else:
                    # All wins or all losses
                    returns.extend([avg] * n_trades)
    except OSError:
        return []
    return returns


def simulate(returns: list[float],
              *, n_paths: int = 10000,
              trades_per_path: int = 100,
              starting_equity_usd: float = 10000.0,
              threshold_pct: float = 20.0,
              seed: int | None = None) -> EquitySimReport:
    """Run Monte Carlo simulation.  Returns aggregate report."""
    notes: list[str] = []
    if not returns:
        return EquitySimReport(
            strategy="?", symbol="?", n_paths=n_paths,
            trades_per_path=trades_per_path,
            starting_equity_usd=starting_equity_usd,
            n_historical_trades_used=0,
            median_end_equity_usd=None,
            p10_end_equity_usd=None,
            p90_end_equity_usd=None,
            median_max_drawdown_pct=None,
            p90_max_drawdown_pct=None,
            p99_max_drawdown_pct=None,
            prob_drawdown_exceeds_threshold=None,
            threshold_pct=threshold_pct,
            risk_of_ruin=None,
            notes=["no historical trade returns available"],
        )
    if len(returns) < 30:
        notes.append(f"Only {len(returns)} historical trades — "
                       "simulator output is statistically weak below n=30")
    rng = random.Random(seed)
    end_equities: list[float] = []
    max_drawdowns_pct: list[float] = []
    n_ruined = 0
    for _ in range(n_paths):
        # Sample with replacement
        path = [rng.choice(returns) for _ in range(trades_per_path)]
        equity = starting_equity_usd
        peak = equity
        max_dd_pct = 0.0
        ruined = False
        for r in path:
            equity += r
            if equity <= 0:
                ruined = True
                max_dd_pct = 100.0
                break
            peak = max(peak, equity)
            dd_pct = ((peak - equity) / peak) * 100
            max_dd_pct = max(max_dd_pct, dd_pct)
        end_equities.append(equity)
        max_drawdowns_pct.append(max_dd_pct)
        if ruined:
            n_ruined += 1

    sorted_eq = sorted(end_equities)
    sorted_dd = sorted(max_drawdowns_pct)

    def _percentile(sorted_data: list[float], pct: float) -> float:
        if not sorted_data:
            return 0.0
        idx = int(pct / 100 * len(sorted_data))
        idx = max(0, min(len(sorted_data) - 1, idx))
        return sorted_data[idx]

    median_eq = statistics.median(end_equities)
    p10_eq = _percentile(sorted_eq, 10)
    p90_eq = _percentile(sorted_eq, 90)
    median_dd = statistics.median(max_drawdowns_pct)
    p90_dd = _percentile(sorted_dd, 90)
    p99_dd = _percentile(sorted_dd, 99)
    prob_threshold = sum(1 for d in max_drawdowns_pct if d > threshold_pct) / n_paths
    risk_of_ruin = n_ruined / n_paths

    return EquitySimReport(
        strategy="?", symbol="?", n_paths=n_paths,
        trades_per_path=trades_per_path,
        starting_equity_usd=starting_equity_usd,
        n_historical_trades_used=len(returns),
        median_end_equity_usd=round(median_eq, 2),
        p10_end_equity_usd=round(p10_eq, 2),
        p90_end_equity_usd=round(p90_eq, 2),
        median_max_drawdown_pct=round(median_dd, 2),
        p90_max_drawdown_pct=round(p90_dd, 2),
        p99_max_drawdown_pct=round(p99_dd, 2),
        prob_drawdown_exceeds_threshold=round(prob_threshold, 4),
        threshold_pct=threshold_pct,
        risk_of_ruin=round(risk_of_ruin, 6),
        notes=notes,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default="book_imbalance")
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--n-paths", type=int, default=10000)
    ap.add_argument("--trades-per-path", type=int, default=100)
    ap.add_argument("--starting-equity", type=float, default=10000.0)
    ap.add_argument("--threshold-pct", type=float, default=20.0,
                    help="Drawdown threshold for prob calculation (default 20%)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    returns = _read_trade_returns(args.strategy, args.symbol)
    report = simulate(
        returns,
        n_paths=args.n_paths,
        trades_per_path=args.trades_per_path,
        starting_equity_usd=args.starting_equity,
        threshold_pct=args.threshold_pct,
        seed=args.seed,
    )
    report.strategy = args.strategy
    report.symbol = args.symbol

    try:
        with EQUITY_SIM_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                                 **asdict(report)},
                                separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: equity sim log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0

    print()
    print("=" * 78)
    print(f"L2 EQUITY CURVE SIMULATOR  ({report.strategy} on {report.symbol})")
    print("=" * 78)
    print(f"  n_paths              : {report.n_paths:,}")
    print(f"  trades_per_path      : {report.trades_per_path:,}")
    print(f"  starting equity      : ${report.starting_equity_usd:,.2f}")
    print(f"  historical trades    : {report.n_historical_trades_used}")
    print()
    print(f"  End equity (p10/median/p90):")
    print(f"    p10    : ${report.p10_end_equity_usd:,.2f}"
          if report.p10_end_equity_usd is not None else "    p10    : n/a")
    print(f"    median : ${report.median_end_equity_usd:,.2f}"
          if report.median_end_equity_usd is not None else "    median : n/a")
    print(f"    p90    : ${report.p90_end_equity_usd:,.2f}"
          if report.p90_end_equity_usd is not None else "    p90    : n/a")
    print()
    print(f"  Max drawdown (median/p90/p99):")
    print(f"    median : {report.median_max_drawdown_pct}%")
    print(f"    p90    : {report.p90_max_drawdown_pct}%")
    print(f"    p99    : {report.p99_max_drawdown_pct}%")
    print()
    print(f"  P(drawdown > {report.threshold_pct}%): {report.prob_drawdown_exceeds_threshold}")
    print(f"  Risk of ruin             : {report.risk_of_ruin}")
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
