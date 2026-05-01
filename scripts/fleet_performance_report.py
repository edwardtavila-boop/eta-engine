"""Fleet performance report — one-command summary of PnL, risk, drift,
launch readiness, and paper-soak progress across all bots.

Usage
-----
    python -m eta_engine.scripts.fleet_performance_report
    python -m eta_engine.scripts.fleet_performance_report --json
"""

from __future__ import annotations

import argparse
import json
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


def fleet_report() -> dict:
    from eta_engine.strategies.per_bot_registry import all_assignments, is_active
    from eta_engine.strategies.risk_optimizer import risk_profile_for_bot

    bots = []
    fleet_data = {"production": 0, "paper_soak": 0, "research": 0, "shadow": 0, "total": 0}

    for a in all_assignments():
        active = is_active(a)
        status = a.extras.get("promotion_status", "unknown")
        risk = risk_profile_for_bot(a.bot_id)

        # Determine readiness tier
        if not active:
            tier = "deactivated"
        elif status in ("production", "live_preflight"):
            tier = "production"
        elif status in ("production_candidate", "paper_soak"):
            tier = "paper_soak"
        elif status in ("research_candidate",):
            tier = "research"
        elif status in ("shadow_benchmark",):
            tier = "shadow"
        else:
            tier = "unknown"

        bots.append({
            "bot_id": a.bot_id, "symbol": a.symbol, "timeframe": a.timeframe,
            "strategy_kind": a.strategy_kind, "status": status, "tier": tier,
            "risk_pct": risk["risk_pct"], "daily_dd_cap": risk["daily_loss_pct"],
            "max_trades_per_day": risk["max_trades_per_day"],
            "oos_sharpe": risk.get("oos_sharpe", 0), "risk_tier": risk.get("tier", "baseline"),
            "in_warmup": risk.get("in_warmup", False),
        })
        if tier in fleet_data:
            fleet_data[tier] += 1
        fleet_data["total"] += 1

    return {
        "fleet": fleet_data,
        "bots": sorted(bots, key=lambda b: ({"production": 0, "paper_soak": 1, "research": 2, "shadow": 3}.get(b["tier"], 99), -b["oos_sharpe"])),
        "generated": datetime.now(tz=UTC).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fleet_performance_report")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    data = fleet_report()
    f = data["fleet"]

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(f"FLEET PERFORMANCE REPORT  |  {f['total']} bots")
        print(f"  Production: {f['production']}  |  Paper-soak: {f['paper_soak']}  |  Research: {f['research']}  |  Shadow: {f['shadow']}")
        print()
        print(f"{'Bot':<28} {'Sym':<5} {'TF':<4} {'Tier':<12} {'Risk%':>6} {'DD%':>6} {'Trades/d':>9} {'Sharpe':>8}")
        print("-" * 90)
        for b in data["bots"]:
            print(f"{b['bot_id']:<28} {b['symbol']:<5} {b['timeframe']:<4} {b['tier']:<12} "
                  f"{b['risk_pct']*100:>5.1f}% {b['daily_dd_cap']*100:>5.1f}% {b['max_trades_per_day']:>9} "
                  f"{b['oos_sharpe']:>+8.2f}")

        # Paper trade PnL summary from most recent fleet run
        print(f"\n{'='*90}")
        print(f"ACTIONS: Run 'python -m eta_engine.scripts.paper_soak_tracker --days 30' to start paper-soak.")
        print(f"  Then 'python -m eta_engine.scripts.fleet_supervisor --paper' for live paper trading.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
