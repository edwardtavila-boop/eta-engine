"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_commission_tier_optimizer
==================================================================
Compute breakeven volume thresholds for IBKR's commission tiers
and tell the operator which tier their projected fleet volume puts
them in.

Why this exists
---------------
IBKR Pro commissions are tiered.  For futures (CME/CBOT/NYMEX/COMEX):
  - <= 1,000 contracts/month  → $0.85 / contract per side
  -    1,001-10,000           → $0.65 / side
  -   10,001-20,000           → $0.45 / side
  -   20,001+                 → $0.25 / side

Plus exchange + regulatory fees (separate, not tiered).  Round-trip
commission is roughly 2× the per-side rate.

For a multi-strategy L2 fleet projected to do 4 strategies × 6 trades
/day × 252 days = 6048 round-trips = ~12k single-side fills per
year, the operator is well into tier 2.  This script:
  1. Reads recent fill volume from broker_fills.jsonl
  2. Projects forward at current pace
  3. Reports current tier, next tier's threshold, dollars saved if
     monthly volume hits next breakpoint

Helps the operator decide whether to:
  - Scale up to hit a higher tier (lower commissions = more
    edge per trade)
  - Stay current pace (good enough)
  - Reduce overhead trades (if tier 1 and trading is marginal)

Run
---
::

    python -m eta_engine.scripts.l2_commission_tier_optimizer
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
TIER_LOG = LOG_DIR / "l2_commission_tier.jsonl"


# IBKR Pro futures tier schedule (approximate; verify in IBKR
# Account Management for exact current rates).  Each tier is (max
# monthly contracts at this rate, per-side commission usd).
IBKR_PRO_FUTURES_TIERS: list[tuple[int, float]] = [
    (1_000,   0.85),
    (10_000,  0.65),
    (20_000,  0.45),
    (100_000, 0.25),
    (10**9,   0.25),  # cap at the lowest rate
]


@dataclass
class TierProjection:
    n_fills_recent_days: int
    recent_window_days: int
    monthly_projected_fills: float
    current_tier_idx: int
    current_per_side_usd: float
    current_monthly_cost_usd: float
    next_tier_threshold: int | None
    fills_needed_for_next_tier: int | None
    next_tier_per_side_usd: float | None
    monthly_savings_if_next_tier_usd: float | None
    annual_savings_if_next_tier_usd: float | None
    notes: list[str] = field(default_factory=list)


def _read_fill_count(*, since_days: int = 30,
                       _path: Path) -> int:
    if not _path.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    n = 0
    try:
        with _path.open("r", encoding="utf-8") as f:
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
                qty = int(rec.get("qty_filled", 0))
                n += qty
    except OSError:
        return 0
    return n


def _tier_for_monthly(monthly_fills: float) -> tuple[int, float]:
    """Return (tier_idx, per_side_usd) for given monthly fill count."""
    for i, (cap, rate) in enumerate(IBKR_PRO_FUTURES_TIERS):
        if monthly_fills <= cap:
            return i, rate
    last = len(IBKR_PRO_FUTURES_TIERS) - 1
    return last, IBKR_PRO_FUTURES_TIERS[last][1]


def compute_tier_projection(*, since_days: int = 30,
                              _fill_path: Path | None = None) -> TierProjection:
    fill_path = _fill_path if _fill_path is not None else BROKER_FILL_LOG
    n_recent = _read_fill_count(since_days=since_days, _path=fill_path)
    # Project to monthly
    monthly = n_recent * (30.0 / max(since_days, 1))
    tier_idx, per_side = _tier_for_monthly(monthly)
    current_cost = monthly * per_side
    notes: list[str] = []
    if n_recent == 0:
        notes.append("no fills in recent window — paper-soak hasn't started "
                       "or broker_fills log is empty")

    # What's the next tier threshold?
    next_threshold: int | None = None
    next_rate: float | None = None
    if tier_idx + 1 < len(IBKR_PRO_FUTURES_TIERS):
        next_threshold = IBKR_PRO_FUTURES_TIERS[tier_idx][0]
        next_rate = IBKR_PRO_FUTURES_TIERS[tier_idx + 1][1]
    fills_to_next = (max(0, next_threshold - int(monthly))
                       if next_threshold is not None else None)
    monthly_savings = None
    annual_savings = None
    if next_rate is not None:
        # Savings = current monthly fills × (current rate - next rate)
        # if operator stays at current volume but with next-tier discount
        monthly_savings = monthly * (per_side - next_rate)
        annual_savings = monthly_savings * 12

    return TierProjection(
        n_fills_recent_days=n_recent,
        recent_window_days=since_days,
        monthly_projected_fills=round(monthly, 1),
        current_tier_idx=tier_idx,
        current_per_side_usd=per_side,
        current_monthly_cost_usd=round(current_cost, 2),
        next_tier_threshold=next_threshold,
        fills_needed_for_next_tier=fills_to_next,
        next_tier_per_side_usd=next_rate,
        monthly_savings_if_next_tier_usd=round(monthly_savings, 2)
                                                if monthly_savings else None,
        annual_savings_if_next_tier_usd=round(annual_savings, 2)
                                                if annual_savings else None,
        notes=notes,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    proj = compute_tier_projection(since_days=args.days)
    try:
        with TIER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                                 **asdict(proj)},
                                separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: tier log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(proj), indent=2))
        return 0

    print()
    print("=" * 78)
    print("L2 COMMISSION TIER OPTIMIZER")
    print("=" * 78)
    print(f"  recent fills ({args.days}d)  : {proj.n_fills_recent_days}")
    print(f"  projected monthly        : {proj.monthly_projected_fills}")
    print(f"  current tier             : tier {proj.current_tier_idx + 1} "
          f"(${proj.current_per_side_usd}/side)")
    print(f"  monthly commission       : ${proj.current_monthly_cost_usd}")
    print()
    if proj.next_tier_threshold:
        print(f"  next tier threshold      : {proj.next_tier_threshold} "
                f"fills/mo")
        print(f"  fills needed for next    : {proj.fills_needed_for_next_tier}")
        print(f"  next-tier rate           : ${proj.next_tier_per_side_usd}/side")
        print(f"  monthly savings (next)   : ${proj.monthly_savings_if_next_tier_usd}")
        print(f"  annual savings (next)    : ${proj.annual_savings_if_next_tier_usd}")
    else:
        print("  Already at the lowest commission tier — no further tier "
                "savings available.")
    if proj.notes:
        print()
        print("  Notes:")
        for n in proj.notes:
            print(f"    - {n}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
