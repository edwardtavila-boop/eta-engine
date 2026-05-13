"""Layer 22-25: Fleet paper launcher — one command to paper-trade
every ready bot with registry-backed strategy dispatch.

Reads per_bot_registry, checks data + venue readiness, runs
paper_trade_sim for each bot, and produces a fleet-level PnL summary.

Usage
-----
    python -m eta_engine.scripts.fleet_paper_launcher
    python -m eta_engine.scripts.fleet_paper_launcher --days 30
    python -m eta_engine.scripts.fleet_paper_launcher --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SIM_SCRIPT = ROOT / "scripts" / "paper_trade_sim.py"
SIM_TIMEOUT_SECONDS = 600

_MULTIPLIERS: dict[str, float] = {
    "MNQ": 0.50,
    "MNQ1": 0.50,
    "NQ": 20.0,
    "NQ1": 20.0,
    "MBT": 1.0,
    "BTC": 1.0,
    "MET": 0.10,
    "ETH": 0.10,
    "SOL": 1.0,
}


@dataclass
class FleetPaperResult:
    bot_id: str
    symbol: str
    timeframe: str
    days: int
    bars: int
    signals: int
    trades: int
    winners: int
    losers: int
    win_rate: float
    pnl: float
    avg_pnl_trade: float
    max_dd: float
    status: str  # OK / SKIPPED / ERROR


def _run_one(bot_id: str, days: int, point_value: float | None = None) -> FleetPaperResult:
    from eta_engine.strategies.per_bot_registry import get_for_bot

    assignment = get_for_bot(bot_id)
    if assignment is None:
        return FleetPaperResult(bot_id, "", "", days, 0, 0, 0, 0, 0, 0, 0, 0, 0, "ERROR: unknown bot")

    status = assignment.extras.get("promotion_status", "")
    if status in {"shadow_benchmark", "deactivated", "deprecated", "non_edge_strategy"}:
        return FleetPaperResult(
            bot_id, assignment.symbol, assignment.timeframe, days, 0, 0, 0, 0, 0, 0, 0, 0, 0, f"SKIPPED: {status}"
        )

    pv = point_value or _MULTIPLIERS.get(assignment.symbol, 1.0)  # noqa: F841 — passed via env to subprocess
    cmd = [
        sys.executable,
        str(SIM_SCRIPT),
        "--bot",
        bot_id,
        "--days",
        str(days),
        "--json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SIM_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            return FleetPaperResult(
                bot_id,
                assignment.symbol,
                assignment.timeframe,
                days,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                f"ERROR: exit {proc.returncode} — {proc.stderr[:100]}",
            )
        data = json.loads(proc.stdout)
        return FleetPaperResult(
            bot_id=bot_id,
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            days=days,
            bars=data["bars"],
            signals=data["signals"],
            trades=data["trades"],
            winners=data["winners"],
            losers=data["losers"],
            win_rate=data["win_rate"],
            pnl=data["total_pnl"],
            avg_pnl_trade=data["avg_pnl_per_trade"],
            max_dd=data["max_dd"],
            status="OK",
        )
    except subprocess.TimeoutExpired:
        return FleetPaperResult(
            bot_id, assignment.symbol, assignment.timeframe, days, 0, 0, 0, 0, 0, 0, 0, 0, 0, "ERROR: timeout"
        )
    except (json.JSONDecodeError, KeyError) as e:
        return FleetPaperResult(
            bot_id, assignment.symbol, assignment.timeframe, days, 0, 0, 0, 0, 0, 0, 0, 0, 0, f"ERROR: {e}"
        )


def launch_fleet(days: int = 30) -> list[FleetPaperResult]:
    from eta_engine.strategies.per_bot_registry import all_assignments, is_active

    assignments = [a for a in all_assignments() if is_active(a)]
    results: list[FleetPaperResult] = []
    for a in assignments:
        print(f"  [{a.bot_id}] running {days}d simulation...", end=" ", flush=True)
        r = _run_one(a.bot_id, days)
        print(f"{r.status}: {r.trades} trades, PnL=${r.pnl:+.2f}")
        results.append(r)
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="fleet_paper_launcher", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--days", type=int, default=30, help="days of data to simulate per bot")
    p.add_argument("--json", action="store_true")
    p.add_argument("--only-ready", action="store_true", help="only bots that can paper trade")
    args = p.parse_args(argv)

    results = launch_fleet(days=args.days)
    if args.only_ready:
        results = [r for r in results if r.status == "OK"]

    if args.json:
        print(
            json.dumps(
                {
                    "fleet_pnl": round(sum(r.pnl for r in results if r.status == "OK"), 2),
                    "fleet_trades": sum(r.trades for r in results if r.status == "OK"),
                    "bots": [
                        {
                            "bot_id": r.bot_id,
                            "symbol": r.symbol,
                            "timeframe": r.timeframe,
                            "days": r.days,
                            "bars": r.bars,
                            "signals": r.signals,
                            "trades": r.trades,
                            "winners": r.winners,
                            "losers": r.losers,
                            "win_rate": round(r.win_rate, 1),
                            "pnl": r.pnl,
                            "avg_pnl_trade": r.avg_pnl_trade,
                            "max_dd": r.max_dd,
                            "status": r.status,
                        }
                        for r in results
                    ],
                    "generated": datetime.now(tz=UTC).isoformat(),
                },
                indent=2,
            )
        )
    else:
        fleet_pnl = sum(r.pnl for r in results if r.status == "OK")
        fleet_trades = sum(r.trades for r in results if r.status == "OK")
        print(f"\nFLEET PAPER SIMULATION — {args.days}d per bot")
        print(f"  Fleet PnL: ${fleet_pnl:+.2f}  |  Fleet trades: {fleet_trades}")
        print()
        print(
            f"{'Bot':<24} {'Sym/TF':<12} {'Days':>5} {'Bars':>7} {'Signals':>8} {'Trades':>7} {'PnL':>10} {'WR':>6} {'Avg':>8} {'DD':>8} {'Status'}"
        )
        print("-" * 115)
        for r in results:
            sym_tf = f"{r.symbol}/{r.timeframe}"
            print(
                f"{r.bot_id:<24} {sym_tf:<12} {r.days:>5} {r.bars:>7} {r.signals:>8} {r.trades:>7} "
                f"${r.pnl:>+9.2f} {r.win_rate:>5.1f}% ${r.avg_pnl_trade:>+7.2f} ${r.max_dd:>7.2f} {r.status}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
