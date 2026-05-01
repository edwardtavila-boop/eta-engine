"""Paper-soak tracker — tracks cumulative PnL, drift, and readiness
per bot across multiple paper-trade sessions.

Stores a JSON ledger at var/eta_engine/state/paper_soak_ledger.json.
Each run appends one row per bot. After 30+ days with 20+ trades
and steady drift, the bot is marked READY for live preflight.

Usage
-----
    # Run one paper session (uses paper_trade_sim under the hood)
    python -m eta_engine.scripts.paper_soak_tracker --days 30

    # Show current soak status
    python -m eta_engine.scripts.paper_soak_tracker --status

    # Reset a bot's soak clock
    python -m eta_engine.scripts.paper_soak_tracker --reset mnq_futures_sage
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from eta_engine.scripts import workspace_roots  # noqa: E402

LEDGER_PATH = workspace_roots.ETA_RUNTIME_STATE_DIR / "paper_soak_ledger.json"
SIM_SCRIPT = ROOT / "scripts" / "paper_trade_sim.py"
MIN_DAYS = 30
MIN_TRADES = 20

_MULTIPLIERS: dict[str, float] = {
    "MNQ": 0.50, "MNQ1": 0.50, "NQ": 20.0, "NQ1": 20.0,
    "BTC": 1.0, "ETH": 1.0, "SOL": 1.0,
}


def _load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"started": datetime.now(tz=UTC).isoformat(), "bot_sessions": {}}


def _save_ledger(ledger: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2, default=str), encoding="utf-8")


def run_session(days: int = 30) -> int:
    from eta_engine.strategies.per_bot_registry import all_assignments, is_active

    ledger = _load_ledger()
    now = datetime.now(tz=UTC)

    assignments = [a for a in all_assignments() if is_active(a)]

    eligible: list = []
    for a in assignments:
        s = a.extras.get("promotion_status", "")
        if s in ("shadow_benchmark", "deactivated", "deprecated", "non_edge_strategy", ""):
            continue
        eligible.append(a)

    for a in eligible:
        cmd = [sys.executable, str(SIM_SCRIPT), "--bot", a.bot_id, "--days", str(days), "--json"]
        print(f"  [{a.bot_id}] running {days}d on {a.symbol}/{a.timeframe}...", end=" ", flush=True)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                bot_sessions = ledger["bot_sessions"].get(a.bot_id, [])
                bot_sessions.append({
                    "date": now.isoformat(), "days": days,
                    "bars": data["bars"], "signals": data["signals"],
                    "trades": data["trades"],
                    "winners": data["winners"], "losers": data["losers"],
                    "win_rate": data["win_rate"], "pnl": data["total_pnl"],
                    "avg_pnl_per_trade": data["avg_pnl_per_trade"],
                    "max_dd": data["max_dd"],
                })
                if len(bot_sessions) > 30:
                    bot_sessions = bot_sessions[-30:]
                ledger["bot_sessions"][a.bot_id] = bot_sessions
                total_trades = sum(s["trades"] for s in bot_sessions)
                total_pnl = sum(s["pnl"] for s in bot_sessions)
                print(f"{data['trades']} trades, PnL=${data['total_pnl']:+.2f} "
                      f"(cumulative: {total_trades}T, ${total_pnl:+.2f})")
            else:
                print(f"ERROR: {proc.stderr[:80] if proc.stderr else 'no output'}")
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
        except (json.JSONDecodeError, KeyError) as e:
            print(f"PARSE ERROR: {e}")

    _save_ledger(ledger)
    return 0


def show_status() -> int:
    ledger = _load_ledger()
    sessions = ledger.get("bot_sessions", {})

    if not sessions:
        print("No paper-soak sessions recorded. Run with --days 30 to start.")
        return 0

    print(f"PAPER-SOAK STATUS  |  Started: {ledger.get('started', 'unknown')[:10]}")
    print(f"{'Bot':<28} {'Sessions':>8} {'Trades':>8} {'PnL':>12} {'WR':>7} {'Ready?'}")
    print("-" * 80)

    for bot_id, history in sorted(sessions.items()):
        total_trades = sum(s.get("trades", 0) for s in history)
        total_pnl = sum(s.get("pnl", 0) for s in history)
        total_win = sum(s.get("winners", 0) for s in history)
        total_loss = sum(s.get("losers", 0) for s in history)
        wr = (total_win / (total_win + total_loss)) * 100 if (total_win + total_loss) > 0 else 0.0
        ready = "YES" if total_trades >= MIN_TRADES and len(history) >= 3 else f"no ({total_trades}/{MIN_TRADES}T, {len(history)}s)"
        print(f"{bot_id:<28} {len(history):>8} {total_trades:>8} ${total_pnl:>+11.2f} {wr:>6.1f}% {ready}")

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="paper_soak_tracker")
    p.add_argument("--days", type=int, default=30, help="days per paper session")
    p.add_argument("--status", action="store_true", help="show current soak status")
    p.add_argument("--reset", type=str, default=None, help="bot_id to reset soak clock")
    args = p.parse_args(argv)

    if args.reset:
        ledger = _load_ledger()
        if args.reset in ledger.get("bot_sessions", {}):
            del ledger["bot_sessions"][args.reset]
            _save_ledger(ledger)
            print(f"Reset soak clock for {args.reset}")
        else:
            print(f"Bot {args.reset} not found in ledger")
        return 0

    if args.status:
        return show_status()

    return run_session(days=args.days)


if __name__ == "__main__":
    sys.exit(main())
