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
    pool: str  # "spot", "futures", "leveraged"
    weight: float  # 0.0 - 1.0 within pool
    capital: float  # absolute capital allocated
    pnl_total: float
    win_rate: float
    sessions: int
    status: str  # "active", "paused", "no_data"
    # Wave-18: tier metadata for prop-fund routing
    tier: str = "TIER_CANDIDATE"  # TIER_PROP_READY / TIER_DIAMOND / TIER_CANDIDATE


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

# DIAMOND BOTS — protected from auto-kill, always get minimum capital.
#
# Stats are R-multiple basis (dimension-free, immune to position-sizing
# bugs that have plagued the USD ledger). Source: canonical dual-source
# trade-closes archive (eta_engine/state/jarvis_intel/trade_closes.jsonl  # HISTORICAL-PATH-OK
# + var/eta_engine/state/jarvis_intel/trade_closes.jsonl, deduped).
# Snapshot 2026-05-12 (wave-14 fleet expansion).
#
# Wave-14 expansion rationale:
# Operator mandate to "conquer futures + commodities + crypto" by
# bringing all paper-soak strategies with strong R-evidence into the
# diamond fleet for data gathering. The promotion gate's hard
# H4_calendar_days=5 requirement is paper-trading-irrelevant — we WANT
# more bots accumulating data, not fewer.
#
# Quarantined / NOT promoted:
#   - mym_sweep_reclaim: corrupt R-values (multiple R=+50/+80/+100 on
#     pnl=$1.25 — same scale-bug pattern as the eur_sweep records the
#     diamond_data_sanitizer quarantined; needs sanitizer pass before
#     promotion can be considered)
#   - mbt_overnight_gap, mbt_rth_orb, mbt_funding_basis,
#     mbt_sweep_reclaim: all trading (n=58-129) but realized_r=0
#     across the board; the R-multiple writer is broken for the MBT
#     family. Must fix the R writer for these bots before they can be
#     R-classified by the watchdog.
DIAMOND_BOTS: set[str] = {
    # ── Tier 1: large-sample sage learners ──────────────────────
    "mnq_futures_sage",  # n=1267 cum_r=+0.82R wr=55%  (marginal-but-large)
    "nq_futures_sage",  # n=1249 cum_r=+0.85R wr=57%  (marginal-but-large)
    # ── Tier 2: confirmed-strong sweep reclaim ──────────────────
    "m2k_sweep_reclaim",  # n=1151 cum_r=+533R  wr=70%  *PROMOTED 2026-05-12* (canonical-data kaizen)
    "eur_sweep_reclaim",  # n= 280 cum_r=+129R  wr=70%  (4/4 sessions positive)
    "mgc_sweep_reclaim",  # n= 158 cum_r= +30R  wr=58%  (wave-3+5 chisel)
    # ── Tier 2 (wave-14: conquer all 3 verticals via IBKR FUTURES) ──
    # Wave-16 mandate (2026-05-12): the diamond fleet is IBKR-FUTURES-ONLY.
    # Alpaca spot is cellared (POOL_SPLIT["spot"]=0.0); Tradovate dormant.
    # Crypto exposure comes from CME micro crypto futures (MET/MBT) routed
    # through IBKR — NOT from BTC/ETH/SOL spot via Alpaca.
    "met_sweep_reclaim",  # n= 208 cum_r=+136R wr=69%  *wave-14* (CME MET futures via IBKR — highest avg_R in fleet)
    "mes_sweep_reclaim_v2",  # n= 416 cum_r=+136R wr=63%  *wave-14* (CME MICRO S&P FUTURES via IBKR)
    "eur_range",  # n= 124 cum_r= +64R wr=71%  *wave-14* (CME 6E EUROFX FUTURES via IBKR)
    "ng_sweep_reclaim",  # n= 243 cum_r= +91R wr=65%  *wave-14* (CME NG NAT GAS FUTURES via IBKR)
    "mes_sweep_reclaim",  # n= 197 cum_r= +56R wr=61%  *wave-14* (CME MICRO S&P FUTURES via IBKR, paired with v2)
    # NOT promoted (wave-16 IBKR-futures-only mandate):
    #   volume_profile_btc — Alpaca SPOT BTC; cellared per POOL_SPLIT.
    #     Strong R-edge (+121R/n=339) but the wrong broker for the
    #     prop-fund routing layer. If/when the operator re-activates
    #     spot crypto (currently POOL_SPLIT["spot"]=0.0), this bot
    #     can be reconsidered.
    # ── Tier 3: small-sample but positive ───────────────────────
    "cl_macro",  # n=   2 cum_r= +2.4R wr=100% (sample too small)
    "gc_momentum",  # n=   8 cum_r= +0.24R wr=50% (R-positive; USD-CRITICAL is a sizing artifact)
    # ── Tier 4: small-sample structurally negative ──────────────
    # These two are net-negative in R-multiples too. Kept under
    # protection because n is too small (4-8) for retirement to be
    # statistically justified. Watch for the n>=20 inflection point.
    "cl_momentum",  # n=   4 cum_r= -1.71R wr=25% (under-baked)
    "mcl_sweep_reclaim",  # n=   8 cum_r= -0.22R wr=50% (flat)
}

# Minimum capital allocation for diamond bots (always active)
DIAMOND_MIN_CAPITAL: float = 2000.0

# Minimum sessions required for allocation
MIN_SESSIONS = 2

# Path to allocation state
ALLOCATION_PATH = Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\capital_allocation.json")

# ────────────────────────────────────────────────────────────────────
# Wave-18: TIER SYSTEM
# ────────────────────────────────────────────────────────────────────
# Diamonds are no longer a flat set — they have tiers that determine
# CAPITAL ROUTING and FAILURE-MODE RESPONSE.
#
#   TIER_PROP_READY  — top 3 by leaderboard composite score; earn real
#                       prop-fund capital allocation.  Eligibility is
#                       data-driven and recomputed each leaderboard run.
#   TIER_DIAMOND     — full diamond fleet with 3-layer protection.
#                       Receive DIAMOND_MIN_CAPITAL floor regardless of
#                       short-term P&L.  Paper-soak data accumulation.
#   TIER_CANDIDATE   — bots not yet diamonds but tracked by the
#                       promotion gate.  No capital floor; receive
#                       performance-weighted allocation only.
#
# The tier is computed dynamically from:
#   - DIAMOND_BOTS membership (TIER_DIAMOND if member)
#   - Leaderboard snapshot's prop_ready_bots set (TIER_PROP_READY upgrade)
#   - Otherwise TIER_CANDIDATE
#
# A bot can be TIER_PROP_READY AND TIER_DIAMOND simultaneously
# (PROP_READY is a SUPERSET of DIAMOND for eligible bots).

TIER_PROP_READY = "TIER_PROP_READY"
TIER_DIAMOND = "TIER_DIAMOND"
TIER_CANDIDATE = "TIER_CANDIDATE"

#: How much real capital each PROP_READY bot gets routed through IBKR.
#: This is a STARTING DEFAULT — operator overrides via the prop-fund
#: control surface once live data warrants scaling up.  Conservative
#: until the bots have proven themselves on real fills.
PROP_READY_CAPITAL_PER_BOT: float = 2500.0

#: Leaderboard receipt path.  capital_allocator reads this to know
#: which bots earned PROP_READY status in the most recent run.
LEADERBOARD_PATH = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state"
    r"\diamond_leaderboard_latest.json",
)


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


def load_prop_ready_bots(
    leaderboard_path: Path = LEADERBOARD_PATH,
) -> frozenset[str]:
    """Read the most recent leaderboard receipt and return the set of
    bots currently designated PROP_READY.

    Returns frozenset() if the receipt is missing or malformed (so the
    allocator never crashes — it just degrades to no-PROP_READY routing
    instead of mis-allocating real capital).
    """
    if not leaderboard_path.exists():
        return frozenset()
    try:
        data = json.loads(leaderboard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    bots = data.get("prop_ready_bots") or []
    if not isinstance(bots, list):
        return frozenset()
    return frozenset(str(b) for b in bots if b)


def get_bot_tier(
    bot_id: str,
    prop_ready: frozenset[str] | None = None,
) -> str:
    """Return the canonical tier for a bot.

    Order of preference: PROP_READY > DIAMOND > CANDIDATE.

    PROP_READY membership is read from the leaderboard receipt
    (load_prop_ready_bots) — caller can pre-fetch to avoid re-reading
    the receipt across many lookups.
    """
    if prop_ready is None:
        prop_ready = load_prop_ready_bots()
    if bot_id in prop_ready:
        return TIER_PROP_READY
    if bot_id in DIAMOND_BOTS:
        return TIER_DIAMOND
    return TIER_CANDIDATE


def is_ibkr_futures_eligible(bot_id: str) -> bool:
    """Return True if this bot's strategy can route through IBKR futures.

    Wave-16 operator mandate (2026-05-12): the prop-fund routing layer is
    IBKR-futures-only.  Alpaca spot is cellared (POOL_SPLIT["spot"]=0.0);
    Tradovate dormant.  Crypto exposure comes from CME micro crypto
    futures (MET/MBT) routed through IBKR — NOT from BTC/ETH/SOL spot
    via Alpaca.

    The PROP_READY badge in diamond_leaderboard requires this gate so
    a high-scoring spot bot doesn't earn real-capital routing through
    a broker the operator has cellared.

    Returns True when classify_pool(bot_id) in ("futures", "leveraged").
    Spot bots return False even if their R-edge is excellent.
    """
    return classify_pool(bot_id) in ("futures", "leveraged")


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
            "symbol": bot_id,
            "pool": pool,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "sessions": len(bot_sessions),
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

        # Wave-18: pre-fetch PROP_READY set once per allocation pass
        prop_ready = load_prop_ready_bots()

        for bot_id, stats in pool_bots.items():
            is_diamond = bot_id in DIAMOND_BOTS
            is_prop_ready = bot_id in prop_ready
            tier = get_bot_tier(bot_id, prop_ready=prop_ready)

            # Wave-18: PROP_READY tier gets a FLOOR of PROP_READY_CAPITAL_PER_BOT
            # ON TOP of any performance-weighted allocation. This is how the
            # elite-3 earn real-capital routing once the operator's prop-fund
            # wiring reads the bot_allocations.tier field.
            if stats["total_pnl"] > 0 and total_profitable_pnl > 0:
                weight = stats["total_pnl"] / total_profitable_pnl
                capital = pool_capital * weight
                if is_prop_ready:
                    capital = max(capital, PROP_READY_CAPITAL_PER_BOT)
                status = "active"
            elif is_prop_ready:
                # PROP_READY ALWAYS-FLOOR (even on -PnL paper window):
                # the leaderboard's eligibility gate (n>=100, avg_r>=+0.20,
                # watchdog non-CRITICAL, sizing non-BREACHED) is more
                # discriminating than total_pnl > 0 — trust it.
                weight = max(0.05, PROP_READY_CAPITAL_PER_BOT / pool_capital)
                capital = PROP_READY_CAPITAL_PER_BOT
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
            # Wave-18: attach tier metadata so the prop-fund routing
            # layer can pick PROP_READY allocations vs DIAMOND vs CANDIDATE.
            ba.tier = tier  # type: ignore[attr-defined]
            allocation.bots[bot_id] = ba
            pool_data["bots"][bot_id] = {
                "weight": weight,
                "capital": capital,
                "pnl_total": stats["total_pnl"],
                "status": status,
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
                "pool": ba.pool,
                "weight": ba.weight,
                "capital": ba.capital,
                "status": ba.status,
                "pnl_total": ba.pnl_total,
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
        print(f"\n{pool_name.upper()} ({POOL_SPLIT[pool_name] * 100:.0f}% = ${pool['capital']:,.0f}):")
        for bid, bd in sorted(pool.get("bots", {}).items(), key=lambda x: -x[1]["pnl_total"]):
            print(
                f"  {bid}: {bd['status']:6s}  weight={bd['weight']:.1%}  "
                f"capital=${bd['capital']:,.0f}  PnL=${bd['pnl_total']:+,.0f}"
            )
