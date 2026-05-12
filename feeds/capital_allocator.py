"""Capital Allocation Engine — futures-first pool system with performance-weighted sizing.

Pools:
  FUTURES (100%): MNQ, NQ, MES, GC, CL, NG, ZN, EUR, MBT, MET via IBKR
  SPOT (0%):       BTC, ETH, SOL bots via Alpaca until capital expands
  LEVERAGED (0%):  retired sleeve; CME micro crypto futures live in FUTURES

Within each pool, capital is allocated by multi-session performance:
  - Positive PnL across sessions → weighted higher
  - Negative PnL → zero allocation (paused)
  - Allocation is proportional to total_pnl among profitable bots
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BotAllocation:
    bot_id: str
    symbol: str
    pool: str          # "spot", "futures", "leveraged"
    weight: float      # 0.0 - 1.0 within pool
    capital: float     # absolute capital allocated
    pnl_total: float
    win_rate: float
    sessions: int
    status: str        # "active", "paused", "no_data"

@dataclass
class PortfolioAllocation:
    total_capital: float
    spot_pool: dict[str, Any] = field(default_factory=dict)
    futures_pool: dict[str, Any] = field(default_factory=dict)
    leveraged_pool: dict[str, Any] = field(default_factory=dict)
    bots: dict[str, BotAllocation] = field(default_factory=dict)

# Asset class → pool mapping
SPOT_SYMBOLS = {"BTC", "ETH", "SOL", "ADA", "AVAX", "LINK", "DOGE"}
FUTURES_SYMBOLS = {"MNQ", "MNQ1", "NQ", "NQ1", "MES", "M2K", "GC", "CL", "NG", "ZN", "6E", "EUR"}
LEVERAGED_SYMBOLS = {"MBT", "MET"}

# Pool allocations — PROD FUND FOCUS
# Primary: futures/commodities on IBKR ($50k prop fund account)
# Secondary: spot crypto on Alpaca (smaller allocation)
POOL_SPLIT = {"futures": 1.0, "spot": 0.0, "leveraged": 0.0}  # leveraged now in futures pool

# DIAMOND BOTS — protected from auto-kill, always get minimum capital
# These are proven profitable across multiple market regimes
DIAMOND_BOTS: set[str] = {
    "mnq_futures_sage",   # +$11,246 across 14 sessions (ROBUST)
    "nq_futures_sage",    # +$2,557 across 7 sessions (ROBUST)
    "cl_momentum",        # +$2,206 across 13 sessions (ROBUST)
    "mcl_sweep_reclaim",  # +$2,197 across 13 sessions (ROBUST)
    "mgc_sweep_reclaim",  # +$853 across 13 sessions (ROBUST)
    "eur_sweep_reclaim",  # +$417 across 13 sessions (FRAGILE)
    "gc_momentum",        # +$142 across 7 sessions (FRAGILE)
    "cl_macro",           # +$1,248 across 7 sessions (confirmed edge)
}

# Minimum capital allocation for diamond bots (always active)
DIAMOND_MIN_CAPITAL: float = 2000.0

# Minimum sessions required for allocation
MIN_SESSIONS = 2

# Path to allocation state
ALLOCATION_PATH = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\capital_allocation.json")


def classify_pool(bot_id: str) -> str:
    """Classify a bot into spot, futures, or leveraged pool by its ID."""
    bid_lower = bot_id.lower()
    # Micro crypto futures (MBT/MET on CME) — part of futures pool
    if any(x in bid_lower for x in ("mbt_", "met_")):
        return "futures"
    # Spot crypto (BTC/ETH/SOL)
    if any(x in bid_lower for x in ("btc_", "eth_", "sol_")):
        # Exclude eth_sweep_reclaim which is futures-like on ETH
        if "perp" in bid_lower or "futures" in bid_lower:
            return "futures"
        return "spot"
    if any(x in bid_lower for x in ("vwap_mr_btc", "volume_profile_btc", "funding_rate_btc")):
        return "spot"
    # Everything else is futures
    return "futures"


def compute_allocations(ledger_path: Path, total_capital: float = 100_000.0) -> PortfolioAllocation:
    """Compute per-bot capital allocations from paper soak ledger data."""
    if not ledger_path.exists():
        return PortfolioAllocation(total_capital=total_capital)

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    sessions = ledger.get("bot_sessions", {})

    allocation = PortfolioAllocation(total_capital=total_capital)

    # Compute per-bot stats
    bot_stats = {}
    for bot_id, bot_sessions in sessions.items():
        if len(bot_sessions) < MIN_SESSIONS:
            continue
        pnls = [s.get("pnl", 0) for s in bot_sessions]
        total_pnl = sum(pnls)
        winners = sum(1 for p in pnls if p > 0)
        win_rate = winners / len(pnls) if pnls else 0
        pool = classify_pool(bot_id)
        bot_stats[bot_id] = {
            "symbol": bot_id, "pool": pool, "total_pnl": total_pnl,
            "win_rate": win_rate, "sessions": len(bot_sessions),
        }

    # Group by pool and compute weights
    for pool_name in ("spot", "futures", "leveraged"):
        pool_bots = {k: v for k, v in bot_stats.items() if v["pool"] == pool_name}
        profitable = {k: v for k, v in pool_bots.items() if v["total_pnl"] > 0}
        total_profitable_pnl = sum(v["total_pnl"] for v in profitable.values())
        pool_capital = total_capital * POOL_SPLIT[pool_name]

        pool_data = {
            "capital": pool_capital,
            "bot_count": len(pool_bots),
            "profitable_count": len(profitable),
            "total_profitable_pnl": total_profitable_pnl,
            "bots": {},
        }

        for bot_id, stats in pool_bots.items():
            is_diamond = bot_id in DIAMOND_BOTS
            if stats["total_pnl"] > 0 and total_profitable_pnl > 0:
                weight = stats["total_pnl"] / total_profitable_pnl
                capital = pool_capital * weight
                status = "active"
            elif is_diamond:
                # DIAMOND PROTECTION: always active with minimum capital
                weight = 0.05  # minimum weight
                capital = max(DIAMOND_MIN_CAPITAL, pool_capital * 0.05)
                status = "active"
            else:
                weight = 0.0
                capital = 0.0
                status = "paused"

            ba = BotAllocation(
                bot_id=bot_id,
                symbol=stats["symbol"],
                pool=pool_name,
                weight=weight,
                capital=capital,
                pnl_total=stats["total_pnl"],
                win_rate=stats["win_rate"],
                sessions=stats["sessions"],
                status=status,
            )
            allocation.bots[bot_id] = ba
            pool_data["bots"][bot_id] = {
                "weight": weight, "capital": capital,
                "pnl_total": stats["total_pnl"], "status": status,
            }

        setattr(allocation, f"{pool_name}_pool", pool_data)

    return allocation


def save_allocation(allocation: PortfolioAllocation, path: Path = ALLOCATION_PATH) -> None:
    """Persist allocation to disk for the supervisor to read."""
    data = {
        "total_capital": allocation.total_capital,
        "spot_pool": allocation.spot_pool,
        "futures_pool": allocation.futures_pool,
        "leveraged_pool": allocation.leveraged_pool,
        "bot_allocations": {
            bid: {
                "pool": ba.pool, "weight": ba.weight, "capital": ba.capital,
                "status": ba.status, "pnl_total": ba.pnl_total,
            }
            for bid, ba in allocation.bots.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_allocation(path: Path = ALLOCATION_PATH) -> PortfolioAllocation | None:
    """Load persisted allocation."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    alloc = PortfolioAllocation(total_capital=data["total_capital"])
    alloc.spot_pool = data.get("spot_pool", {})
    alloc.futures_pool = data.get("futures_pool", {})
    alloc.leveraged_pool = data.get("leveraged_pool", {})
    for bid, ba_data in data.get("bot_allocations", {}).items():
        alloc.bots[bid] = BotAllocation(
            bot_id=bid,
            symbol=ba_data.get("symbol", "?"),
            pool=ba_data["pool"],
            weight=ba_data["weight"],
            capital=ba_data["capital"],
            pnl_total=ba_data["pnl_total"],
            win_rate=0.0,
            sessions=0,
            status=ba_data["status"],
        )
    return alloc


def get_bot_capital(bot_id: str, path: Path = ALLOCATION_PATH) -> float:
    """Get allocated capital for a bot. Returns 0 if paused/no-data."""
    alloc = load_allocation(path)
    if alloc and bot_id in alloc.bots:
        return alloc.bots[bot_id].capital
    return 0.0


def _read_registry_map() -> dict[str, dict[str, str]]:
    """Parse per_bot_registry for bot->symbol mapping."""
    import re
    reg_path = Path(r"C:\EvolutionaryTradingAlgo\eta_engine\strategies\per_bot_registry.py")
    reg_map = {}
    if reg_path.exists():
        content = reg_path.read_text(encoding="utf-8")
        for m in re.finditer(
            r'"(\w+)"\s*:\s*BotAssignment\(\s*symbol\s*=\s*"(\w+)"',
            content,
        ):
            reg_map[m.group(1)] = {"symbol": m.group(2)}
    return reg_map


if __name__ == "__main__":
    # Compute and save allocations from current soak data
    import sys
    ledger = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\paper_soak_ledger.json")
    total = float(sys.argv[1]) if len(sys.argv) > 1 else 100_000.0
    alloc = compute_allocations(ledger, total)
    save_allocation(alloc)
    print(f"Allocation saved to {ALLOCATION_PATH}")
    print(f"Total capital: ${total:,.0f}")
    for pool_name in ("spot", "futures", "leveraged"):
        pool = getattr(alloc, f"{pool_name}_pool")
        print(f"\n{pool_name.upper()} ({POOL_SPLIT[pool_name]*100:.0f}% = ${pool['capital']:,.0f}):")
        for bid, bd in sorted(pool.get("bots", {}).items(), key=lambda x: -x[1]["pnl_total"]):
            print(
                f"  {bid}: {bd['status']:6s}  weight={bd['weight']:.1%}  "
                f"capital=${bd['capital']:,.0f}  PnL=${bd['pnl_total']:+,.0f}"
            )
