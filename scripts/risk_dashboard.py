"""Risk dashboard — per-bot risk utilization, equity curve, drawdown state.
Reads the decision journal and registry to surface the fleet risk surface.

Usage
-----
    python -m eta_engine.scripts.risk_dashboard
    python -m eta_engine.scripts.risk_dashboard --json
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


def risk_dashboard() -> dict:
    from eta_engine.strategies.per_bot_registry import all_assignments, is_active
    from eta_engine.strategies.risk_optimizer import risk_profile_for_bot

    bots = []
    fleet_risk_total = 0.0
    fleet_dd_total = 0.0

    for a in all_assignments():
        if not is_active(a):
            continue
        profile = risk_profile_for_bot(a.bot_id)
        status = a.extras.get("promotion_status", "unknown")
        bots.append({
            "bot_id": a.bot_id,
            "symbol": a.symbol,
            "timeframe": a.timeframe,
            "strategy_kind": a.strategy_kind,
            "status": status,
            "risk_pct": profile["risk_pct"],
            "daily_loss_pct": profile["daily_loss_pct"],
            "max_trades_per_day": profile["max_trades_per_day"],
            "oos_sharpe": profile.get("oos_sharpe", 0),
            "tier": profile.get("tier", "baseline"),
            "in_warmup": profile.get("in_warmup", False),
        })
        fleet_risk_total += profile["risk_pct"]
        fleet_dd_total += profile["daily_loss_pct"]

    return {
        "fleet": {
            "total_bots": len(bots),
            "total_risk_allocated_pct": round(fleet_risk_total, 4),
            "fleet_dd_cap_pct": round(fleet_dd_total / len(bots), 4) if bots else 0,
            "fleet_daily_budget_pct": 3.5,
        },
        "risk_matrix": {
            "elite": [b for b in bots if b["tier"] == "elite"],
            "strong": [b for b in bots if b["tier"] == "strong"],
            "baseline": [b for b in bots if b["tier"] == "baseline"],
            "research": [b for b in bots if b["tier"] == "research"],
        },
        "bots": bots,
        "generated": datetime.now(tz=UTC).isoformat(),
    }


def _color(tier: str) -> str:
    return {"elite": "ELITE ", "strong": "STRONG", "baseline": "BASE  ", "research": "RESRCH"}.get(tier, tier)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="risk_dashboard")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    data = risk_dashboard()

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        f = data["fleet"]
        print(f"RISK DASHBOARD  |  {f['total_bots']} bots  |  Fleet DD cap: {f['fleet_daily_budget_pct']*100:.1f}%")
        print(f"  Allocated risk: {f['total_risk_allocated_pct']*100:.1f}% of equity across {f['total_bots']} bots")
        print()
        print(f"{'Bot':<28} {'Symbol':<6} {'Status':<22} {'Tier':<8} {'Risk%':>6} {'DD cap%':>8} {'Trades/day':>11} {'Sharpe':>8}")
        print("-" * 110)
        for b in data["bots"]:
            print(f"{b['bot_id']:<28} {b['symbol']:<6} {b['status']:<22} {_color(b['tier']):<8} "
                  f"{b['risk_pct']*100:>5.1f}% {b['daily_loss_pct']*100:>7.1f}% {b['max_trades_per_day']:>11} "
                  f"{b['oos_sharpe']:>+8.2f}")

        # Risk matrix breakdown
        print("\nRisk matrix:")
        for tier in ["elite", "strong", "baseline", "research"]:
            bots_in_tier = data["risk_matrix"][tier]
            if bots_in_tier:
                names = ", ".join(b["bot_id"] for b in bots_in_tier)
                rp = bots_in_tier[0]["risk_pct"] * 100
                dp = bots_in_tier[0]["daily_loss_pct"] * 100
                print(f"  {tier.upper():<10} ({rp:.1f}% risk, {dp:.1f}% DD cap): {names}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
