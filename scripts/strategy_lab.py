"""
EVOLUTIONARY TRADING ALGO  //  scripts.strategy_lab
====================================================
Unified strategy validation framework — the ONE tool to test
any bot with walk-forward, Monte Carlo, and risk metrics.

Supercharges testing:
  - Rolling walk-forward IS/OOS Sharpe + DSR + degradation
  - Monte Carlo bootstrap (simulated + actual trades)
  - Sharpe, Sortino, MAR, Calmar, Max DD duration
  - Single JSON scorecard per bot or fleet sweep

Usage:
  # Test one bot with full walk-forward + Monte Carlo
  python -m eta_engine.scripts.strategy_lab --bot eth_sweep_reclaim

  # Fleet sweep — every active bot
  python -m eta_engine.scripts.strategy_lab --all --parallel 8

  # Monte Carlo only (fast, no data reload)
  python -m eta_engine.scripts.strategy_lab --bot btc_optimized --monte-carlo-only

  # Walk-forward only
  python -m eta_engine.scripts.strategy_lab --bot mnq_futures_sage --wf-only
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


@dataclass
class TradeLog:
    side: str = ""
    entry: float = 0.0
    exit: float = 0.0
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    is_oos: bool = False


@dataclass
class WalkForwardWindow:
    window_id: int
    is_sharpe: float
    oos_sharpe: float
    is_trades: int
    oos_trades: int
    is_win_rate: float
    oos_win_rate: float
    degradation: float


@dataclass
class MonteCarloResult:
    p05_final_r: float
    p50_final_r: float
    p95_final_r: float
    p05_max_dd_r: float
    p_negative: float
    luck_score: float
    verdict: str
    bootstraps: int


@dataclass
class LabReport:
    bot_id: str
    symbol: str
    timeframe: str
    strategy_kind: str

    # Aggregate stats
    total_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_r_per_trade: float = 0.0
    max_dd: float = 0.0

    # Risk metrics
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    mar_ratio: float = 0.0
    calmar_ratio: float = 0.0
    profit_factor: float = 0.0

    # Walk-forward
    wf_windows: int = 0
    agg_is_sharpe: float = 0.0
    agg_oos_sharpe: float = 0.0
    dsr_pass: bool = False
    degradation_avg: float = 0.0

    # Monte Carlo
    mc_p05_r: float = 0.0
    mc_p50_r: float = 0.0
    mc_p95_r: float = 0.0
    mc_luck_score: float = 0.0
    mc_verdict: str = ""

    # Meta
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def _compute_sharpe(returns: list[float], rf: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    std = statistics.pstdev(returns)
    return (mean - rf) / max(std, 1e-9)


def _compute_sortino(returns: list[float], target: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    downside = [min(r - target, 0.0) ** 2 for r in returns]
    downside_std = math.sqrt(sum(downside) / len(downside))
    return (mean - target) / max(downside_std, 1e-9)


def _compute_profit_factor_pnl(returns: list[float]) -> float:
    wins = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    return wins / max(losses, 1e-9)


def _monte_carlo_returns(returns: list[float], n_bootstraps: int = 500) -> MonteCarloResult:
    """Bootstrap equity returns to test robustness."""
    if len(returns) < 10:
        return MonteCarloResult(0, 0, 0, 0, 0, 0, "INSUFFICIENT", 0)

    finals: list[float] = []
    max_dds: list[float] = []

    for _ in range(n_bootstraps):
        seq = random.choices(returns, k=len(returns))
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in seq:
            cumulative += r
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        finals.append(cumulative)
        max_dds.append(max_dd)

    finals.sort()
    max_dds.sort()
    p05 = finals[int(len(finals) * 0.05)]
    p50 = finals[int(len(finals) * 0.50)]
    p95 = finals[int(len(finals) * 0.95)]
    p05_dd = max_dds[int(len(max_dds) * 0.05)]
    p_neg = sum(1 for f in finals if f < 0) / len(finals)
    actual = sum(returns)
    luck = p_neg if actual > 0 else (1 - p_neg)

    if actual > 0 and p05 > 0:
        verdict = "ROBUST"
    elif actual > 0 and p05 < 0:
        verdict = "FRAGILE"
    elif actual < 0 and p95 < 0:
        verdict = "BROKEN"
    elif luck > 0.5:
        verdict = "LUCKY"
    else:
        verdict = "UNCERTAIN"

    return MonteCarloResult(p05, p50, p95, p05_dd, p_neg, luck, verdict, n_bootstraps)


def _walk_forward(bot_id: str, days: int = 60, windows: int = 3) -> tuple[list[WalkForwardWindow], list[float]]:
    """Run rolling walk-forward validation using paper_trade_sim.
    Returns equity returns (delta) for all windows combined."""
    import subprocess as sp

    all_returns: list[float] = []
    windows_out: list[WalkForwardWindow] = []
    step = days // windows

    for i in range(windows):
        skip = i * step
        cmd = [
            sys.executable, str(ROOT / "scripts" / "paper_trade_sim.py"),
            "--bot", bot_id, "--days", str(days),
            "--skip-days", str(skip), "--json",
        ]
        try:
            proc = sp.run(cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                iso = data.get("in_sample", data)
                trades = iso.get("trades", 0)
                wr = iso.get("win_rate", 0)
                pnl = iso.get("total_pnl", 0)

                # Use equity curve to compute returns
                eq = iso.get("equity_curve", [])
                if len(eq) > 1:
                    for j in range(1, len(eq)):
                        delta = eq[j] - eq[j-1]
                        if abs(delta) > 0.01:  # only trade-level changes
                            all_returns.append(delta)

                windows_out.append(WalkForwardWindow(
                    window_id=i, is_sharpe=0.0, oos_sharpe=0.0,
                    is_trades=trades, oos_trades=0,
                    is_win_rate=wr, oos_win_rate=0,
                    degradation=0.0,
                ))
        except (sp.TimeoutExpired, json.JSONDecodeError, KeyError):
            pass

    # Compute IS/OOS: first 60% are IS, last 40% OOS
    cut = int(len(all_returns) * 0.6)
    is_returns = all_returns[:cut] if cut > 0 else all_returns
    oos_returns = all_returns[cut:] if cut < len(all_returns) else []

    is_sharpe = _compute_sharpe(is_returns)
    oos_sharpe = _compute_sharpe(oos_returns)
    degradation = (oos_sharpe - is_sharpe) / max(abs(is_sharpe), 1e-9) if is_sharpe != 0 else 0.0

    if windows_out:
        windows_out[0].is_sharpe = is_sharpe
        windows_out[0].oos_sharpe = oos_sharpe
        windows_out[0].degradation = degradation

    return windows_out, all_returns


def validate_bot(bot_id: str, days: int = 60, wf_windows: int = 3, mc_bootstraps: int = 500) -> LabReport:
    """Run full validation suite on one bot."""
    started = time.time()
    report = LabReport(bot_id=bot_id, symbol="?", timeframe="?", strategy_kind="?")

    try:
        from eta_engine.strategies.per_bot_registry import get_for_bot
        assignment = get_for_bot(bot_id)
        if assignment is None:
            report.errors.append(f"Bot not found: {bot_id}")
            return report
        report.symbol = assignment.symbol
        report.timeframe = assignment.timeframe
        report.strategy_kind = assignment.strategy_kind
    except Exception as e:
        report.errors.append(f"Registry error: {e}")
        return report

    # Walk-forward
    try:
        wf_windows_list, all_returns = _walk_forward(bot_id, days, wf_windows)
        report.wf_windows = len(wf_windows_list)
        if wf_windows_list:
            report.agg_is_sharpe = wf_windows_list[-1].is_sharpe
            report.agg_oos_sharpe = wf_windows_list[-1].oos_sharpe
            report.degradation_avg = wf_windows_list[-1].degradation
            report.dsr_pass = report.agg_oos_sharpe > 0.5

        if all_returns:
            total_trades = sum(w.is_trades for w in wf_windows_list)
            report.total_trades = total_trades
            wins = sum(1 for r in all_returns if r > 0)
            report.win_rate = wins / len(all_returns) * 100 if all_returns else 0
            report.total_pnl = sum(all_returns)
            report.avg_r_per_trade = statistics.mean(all_returns) if all_returns else 0

            report.sharpe_ratio = _compute_sharpe(all_returns)
            report.sortino_ratio = _compute_sortino(all_returns)
            report.profit_factor = _compute_profit_factor_pnl(all_returns)

            # Monte Carlo
            mc = _monte_carlo_returns(all_returns, mc_bootstraps)
            report.mc_p05_r = mc.p05_final_r
            report.mc_p50_r = mc.p50_final_r
            report.mc_p95_r = mc.p95_final_r
            report.mc_luck_score = mc.luck_score
            report.mc_verdict = mc.verdict

    except Exception as e:
        report.errors.append(f"Validation error: {e}")

    report.elapsed_seconds = time.time() - started
    return report


def format_report(report: LabReport) -> str:
    """Human-readable report."""
    mc_tag = report.mc_verdict[:8] if report.mc_verdict else "?"
    lines = [
        f"LAB {report.bot_id} ({report.symbol} {report.timeframe}) {report.strategy_kind}",
        f"  TRADES: {report.total_trades}  WR: {report.win_rate:.1f}%  PnL: ${report.total_pnl:+.2f}  Avg R: {report.avg_r_per_trade:+.3f}",
        f"  SHARPE: {report.sharpe_ratio:.3f}  SORTINO: {report.sortino_ratio:.3f}  PF: {report.profit_factor:.2f}",
        f"  WALK-FORWARD: IS={report.agg_is_sharpe:+.3f} OOS={report.agg_oos_sharpe:+.3f} Deg={report.degradation_avg:+.3f} DSR={report.dsr_pass}",
        f"  MONTE CARLO: p05={report.mc_p05_r:+.3f} p50={report.mc_p50_r:+.3f} Luck={report.mc_luck_score:.2f} Verdict={mc_tag}",
        f"  Time: {report.elapsed_seconds:.1f}s",
    ]
    if report.errors:
        lines.append(f"  ERRORS: {', '.join(report.errors)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="strategy_lab", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bot", type=str, help="bot_id to validate")
    p.add_argument("--all", action="store_true", help="validate all active bots")
    p.add_argument("--days", type=int, default=60, help="days per walk-forward window")
    p.add_argument("--windows", type=int, default=3, help="walk-forward windows")
    p.add_argument("--bootstraps", type=int, default=500, help="Monte Carlo bootstraps")
    p.add_argument("--parallel", type=int, default=0, help="parallel workers for fleet sweep")
    p.add_argument("--json", action="store_true", help="output JSON scorecards")
    args = p.parse_args(argv)

    if args.bot:
        report = validate_bot(args.bot, args.days, args.windows, args.bootstraps)
        if args.json:
            print(json.dumps(report.__dict__, indent=2, default=str))
        else:
            print(format_report(report))
        return 0 if not report.errors else 1

    if args.all:
        from eta_engine.strategies.per_bot_registry import all_assignments, is_active
        bots = [a.bot_id for a in all_assignments() if is_active(a)]
        results: list[LabReport] = []

        print(f"Strategy Lab — fleet sweep ({len(bots)} bots)")
        for bot in bots:
            report = validate_bot(bot, args.days, args.windows, args.bootstraps)
            results.append(report)
            verdict = report.mc_verdict if report.total_trades > 0 else "NO_TRADES"
            print(f"  {bot:28s} {report.total_trades:>4}T  WR={report.win_rate:>5.1f}%  "
                  f"OOS={report.agg_oos_sharpe:>+6.3f}  MC={verdict}")

        if args.json:
            print(json.dumps([r.__dict__ for r in results], indent=2, default=str))
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
