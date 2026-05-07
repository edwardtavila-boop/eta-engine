"""Fleet sweep -- validate all bots from paper soak ledger only (no subprocess fallback)."""
import json
import math
import statistics
import time
from pathlib import Path

LEDGER_PATH = Path("var/eta_engine/state/paper_soak_ledger.json")
REGISTRY_PATH = Path("eta_engine/strategies/per_bot_registry.py")


def read_registry_map() -> dict[str, dict[str, str]]:
    """Parse per_bot_registry for bot->symbol/tf/strategy mapping without importing."""
    reg_map: dict[str, dict[str, str]] = {}
    if not REGISTRY_PATH.exists():
        return reg_map
    content = REGISTRY_PATH.read_text(encoding="utf-8")
    # Simple parser: find per-bot entries
    import re
    # Match patterns like: "btc_optimized": BotAssignment(symbol="BTC", ...
    for m in re.finditer(
        r'"(\w+)"\s*:\s*BotAssignment\(\s*symbol\s*=\s*"(\w+)"[^)]*timeframe\s*=\s*"(\w+)"[^)]*strategy_kind\s*=\s*"([^"]+)"',
        content
    ):
        reg_map[m.group(1)] = {
            "symbol": m.group(2),
            "tf": m.group(3),
            "strategy": m.group(4),
        }
    return reg_map


def compute_sharpe(returns: list[float], rf: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    stdev = statistics.stdev(returns) if len(returns) >= 2 else 0.01
    if stdev < 1e-9:
        return 0.0
    daily_rf = rf / 252
    return (mean - daily_rf) / stdev


def compute_sortino(returns: list[float], rf: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    neg = [r - rf/252 for r in returns if r < rf/252]
    if not neg:
        return mean * 20 if mean > 0 else 0.0
    downside = math.sqrt(sum(x*x for x in neg) / len(returns))
    if downside < 1e-9:
        return 0.0
    return (mean - rf/252) / downside


def compute_profit_factor(pnls: list[float]) -> float:
    gross_profit = sum(r for r in pnls if r > 0)
    gross_loss = abs(sum(r for r in pnls if r < 0))
    if gross_loss < 1e-9:
        return gross_profit * 10 if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def monte_carlo(returns: list[float], n_bootstraps: int = 500) -> dict[str, object]:
    if len(returns) < 5:
        return {"p05": 0, "p50": 0, "p95": 0, "p_neg": 0, "verdict": "INSUFFIC"}
    import random
    actual_total = sum(returns)
    final_vals = []
    for _ in range(n_bootstraps):
        sample = random.choices(returns, k=len(returns))
        final_vals.append(sum(sample))
    final_vals.sort()
    n = len(final_vals)
    p05 = final_vals[int(n * 0.05)]
    p50 = final_vals[int(n * 0.50)]
    p95 = final_vals[int(n * 0.95)]
    p_neg = sum(1 for v in final_vals if v < 0) / n

    # Luck score: how much actual beats median
    luck = (actual_total - p50) / abs(p50) if p50 != 0 else 1.0 if actual_total > 0 else -1.0

    if p_neg < 0.05:
        verdict = "ROBUST"
    elif actual_total > 0 and p05 < 0:
        verdict = "FRAGILE"
    elif actual_total < 0 and p95 < 0:
        verdict = "BROKEN"
    elif luck > 0.5:
        verdict = "LUCKY"
    else:
        verdict = "UNCERTAIN"

    return {"p05": p05, "p50": p50, "p95": p95, "p_neg": p_neg, "verdict": verdict}


def main() -> int:
    if not LEDGER_PATH.exists():
        print("No paper soak ledger found.")
        return 1

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    sessions_by_bot = ledger.get("bot_sessions", {})
    bot_ids = sorted(sessions_by_bot.keys())
    registry = read_registry_map()

    print(f"=== FLEET SWEEP: {len(bot_ids)} bots (ledger-only, no subprocess) ===")
    print()

    results = []
    t0 = time.time()

    for bot_id in bot_ids:
        sessions = sessions_by_bot[bot_id]
        reg = registry.get(bot_id, {})
        symbol = reg.get("symbol", "?")
        strategy = reg.get("strategy", "?")

        # Extract PnL from sessions
        pnls = [s.get("pnl", 0.0) for s in sessions if abs(s.get("pnl", 0.0)) > 0.01]
        total_pnl = sum(pnls)
        n_sessions = len(sessions)
        n_trades = sum(s.get("trades", 0) for s in sessions)

        wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100 if pnls else 0.0

        sharpe = compute_sharpe(pnls) if len(pnls) >= 2 else 0.0
        sortino = compute_sortino(pnls) if len(pnls) >= 2 else 0.0
        pf = compute_profit_factor(pnls) if pnls else 0.0
        mc = monte_carlo(pnls, 500)

        flag = ""
        if n_sessions < 2:
            flag = " [THIN]"
        elif total_pnl > 0 and sharpe > 0.5:
            flag = " ** DIAMOND **"
        elif total_pnl > 0:
            flag = " +"
        elif total_pnl < 0:
            flag = " -"

        results.append({
            "bot_id": bot_id, "symbol": symbol, "strategy": strategy,
            "total_pnl": total_pnl, "wr": wr, "sharpe": sharpe,
            "sortino": sortino, "pf": pf, "mc": mc,
            "n_sessions": n_sessions, "n_trades": n_trades,
            "flag": flag,
        })

        print(f"  {bot_id:<28} {symbol:<6} {strategy[:20]:<20}"
              f" PnL={total_pnl:+8.2f} WR={wr:5.1f}%"
              f" Sharpe={sharpe:+6.2f} PF={pf:6.2f}"
              f" MC={mc['verdict']:<8} {n_sessions}sess {n_trades}trades{flag}")

    elapsed = time.time() - t0

    # Summaries
    diamonds = [r for r in results if "DIAMOND" in r["flag"]]
    profitable = [r for r in results if r["total_pnl"] > 0]
    losers = [r for r in results if r["total_pnl"] < 0]
    thin = [r for r in results if r["n_sessions"] < 2]

    fleet_pnl = sum(r["total_pnl"] for r in results)

    print()
    print(f"=== SUMMARY ({elapsed:.1f}s) ===")
    print(f"  Total bots:       {len(results)}")
    print(f"  DIAMOND:           {len(diamonds)} (PnL>0, Sharpe>0.5)")
    print(f"  Profitable:       {len(profitable)}")
    print(f"  Losing:           {len(losers)}")
    print(f"  Thin data (<2s):  {len(thin)}")
    print(f"  Fleet PnL:        ${fleet_pnl:+.2f}")
    print("  Next action:      Reset ledger + soak 7-14 days for thick data")

    if diamonds:
        print(f"  Diamonds:         {', '.join(r['bot_id'] for r in diamonds)}")
    if losers:
        worst = sorted(losers, key=lambda x: x["total_pnl"])[:5]
        worst_strs = []
        for r in worst:
            worst_strs.append(f'{r["bot_id"]}(${r["total_pnl"]:+.0f})')
        print(f"  Worst losers:     {', '.join(worst_strs)}")

    # Auto-generate capital allocation config
    try:
        from eta_engine.feeds.capital_allocator import compute_allocations, save_allocation
        alloc = compute_allocations(LEDGER_PATH)
        save_allocation(alloc)
        active_bots = sum(1 for b in alloc.bots.values() if b.status == "active")
        print(f"  Capital allocation: {active_bots} active bots, config saved")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    main()
