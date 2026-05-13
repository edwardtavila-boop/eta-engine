"""Layer 12: Paper-trade PnL tracker. Reads the decision journal
and surfaces per-bot trade count, PnL, win rate, and equity curve.

Intended for the Command Center dashboard fleet-equity card.

Usage
-----
    python -m eta_engine.scripts.paper_pnl_tracker
    python -m eta_engine.scripts.paper_pnl_tracker --json
    python -m eta_engine.scripts.paper_pnl_tracker --since-days 7
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from eta_engine.scripts import workspace_roots  # noqa: E402


@dataclass
class BotPnl:
    bot_id: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    net_pnl_usd: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    avg_r_per_trade: float


def load_journal_events(since_days: int = 90) -> list[dict[str, Any]]:
    jpath = workspace_roots.ETA_RUNTIME_DECISION_JOURNAL_PATH
    if not jpath.exists():
        return []
    events = []
    cutoff = datetime.now(tz=UTC) - timedelta(days=since_days)
    with jpath.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                ts = event.get("ts", "")
                if ts:
                    dt = datetime.fromisoformat(ts)
                    if dt >= cutoff:
                        events.append(event)
            except (json.JSONDecodeError, ValueError):
                continue
    return events


def compute_bot_pnl(events: list[dict[str, Any]], bot_filter: str | None = None) -> list[BotPnl]:
    by_bot: dict[str, dict] = {}
    for e in events:
        meta = e.get("metadata", {})
        bot_id = meta.get("bot_id", e.get("intent", "").replace("dispatch_", ""))
        if bot_filter and bot_id != bot_filter:
            continue
        if bot_id not in by_bot:
            by_bot[bot_id] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "profit": 0.0, "loss": 0.0, "r_sum": 0.0}
        rec = by_bot[bot_id]
        rec["trades"] += 1
        pnl = float(meta.get("pnl_usd", 0.0))
        rec["pnl"] += pnl
        rec["r_sum"] += float(meta.get("realized_r", 0.0))
        if pnl > 0:
            rec["wins"] += 1
            rec["profit"] += pnl
        elif pnl < 0:
            rec["losses"] += 1
            rec["loss"] += abs(pnl)

    results: list[BotPnl] = []
    for bot_id, rec in sorted(by_bot.items()):
        if rec["trades"] < 1:
            continue
        wr = (rec["wins"] / rec["trades"]) * 100 if rec["trades"] > 0 else 0.0
        pf = rec["profit"] / rec["loss"] if rec["loss"] > 0 else float("inf")
        avg_r = rec["r_sum"] / rec["trades"] if rec["trades"] > 0 else 0.0
        results.append(
            BotPnl(
                bot_id=bot_id,
                total_trades=rec["trades"],
                winning_trades=rec["wins"],
                losing_trades=rec["losses"],
                win_rate_pct=round(wr, 1),
                net_pnl_usd=round(rec["pnl"], 2),
                gross_profit=round(rec["profit"], 2),
                gross_loss=round(rec["loss"], 2),
                profit_factor=round(pf, 3),
                avg_r_per_trade=round(avg_r, 4),
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="paper_pnl_tracker")
    p.add_argument("--json", action="store_true")
    p.add_argument("--since-days", type=int, default=90)
    p.add_argument("--bot", type=str, default=None)
    args = p.parse_args(argv)

    events = load_journal_events(since_days=args.since_days)
    pnls = compute_bot_pnl(events, bot_filter=args.bot)

    if args.json:
        fleet_total = sum(b.net_pnl_usd for b in pnls)
        fleet_trades = sum(b.total_trades for b in pnls)
        print(
            json.dumps(
                {
                    "fleet_pnl": round(fleet_total, 2),
                    "fleet_trades": fleet_trades,
                    "bots": [
                        {
                            "bot_id": b.bot_id,
                            "trades": b.total_trades,
                            "win_rate": b.win_rate_pct,
                            "pnl": b.net_pnl_usd,
                            "profit_factor": b.profit_factor,
                            "avg_r": b.avg_r_per_trade,
                        }
                        for b in pnls
                    ],
                    "generated": datetime.now(tz=UTC).isoformat(),
                },
                indent=2,
            )
        )
    else:
        if not pnls:
            print("No paper trades recorded yet. Start a paper-trade session to populate the journal.")
            return 0
        fleet_total = sum(b.net_pnl_usd for b in pnls)
        fleet_trades = sum(b.total_trades for b in pnls)
        print(f"FLEET PAPER PNL — total={fleet_trades} trades, net={fleet_total:+.2f} USD")
        print(f"\n{'Bot':<24} {'Trades':>6} {'Win%':>7} {'PnL':>10} {'PF':>8} {'Avg R':>8}")
        print("-" * 70)
        for b in pnls:
            print(
                f"{b.bot_id:<24} {b.total_trades:>6} {b.win_rate_pct:>6.1f}% "
                f"{b.net_pnl_usd:>+10.2f} {b.profit_factor:>8.3f} {b.avg_r_per_trade:>+8.4f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
