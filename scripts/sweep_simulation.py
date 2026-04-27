"""
EVOLUTIONARY TRADING ALGO  //  scripts.sweep_simulation
============================================
Tier-A equity -> 60 / 30 / 10 sweep allocation projection.

Once MNQ + NQ (Tier-A) reach $M in live-tiny equity, the plan is to sweep:
    60% -> MNQ compounder     (retain in futures engine)
    30% -> ETH/SOL/XRP perps  (Tier-B graduates)
    10% -> grid/DCA (crypto_seed)

This script takes a starting equity + weekly expected return, simulates the
sweep over N weeks, and produces per-bucket final balance + per-week timeline.

Usage
-----
    python -m eta_engine.scripts.sweep_simulation --start 27000 --weeks 12
    python -m eta_engine.scripts.sweep_simulation --start 50000 --weeks 26 \
        --mnq-wr 0.55 --mnq-exp 0.45 --perp-wr 0.50 --perp-exp 0.30 \
        --grid-wr 0.50 --grid-exp 0.12

Outputs
-------
- docs/sweep_sim_report.json  — per-week timeline + bucket finals
- docs/sweep_sim_tearsheet.txt — 100-col text summary
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


@dataclass
class Bucket:
    name: str
    allocation_pct: float
    trades_per_week: float
    win_rate: float
    expectancy_r: float
    risk_per_trade_pct: float
    equity: float = 0.0
    trades_cum: int = 0
    pnl_cum: float = 0.0
    history: list[dict] = field(default_factory=list)


def _week_pnl(
    equity: float,
    trades_per_week: float,
    expectancy_r: float,
    risk_per_trade_pct: float,
    win_rate: float,
) -> tuple[float, int]:
    """Return (pnl_usd, trades_taken) for one week at the given params.

    Uses expected-value: pnl ≈ trades * expectancy_r * (risk_per_trade_pct * equity)
    """
    n = max(0, int(round(trades_per_week)))
    if n == 0 or equity <= 0:
        return 0.0, 0
    dollar_risk = equity * risk_per_trade_pct
    pnl = n * expectancy_r * dollar_risk
    return pnl, n


def simulate(
    start_equity: float,
    weeks: int,
    mnq: Bucket,
    perp: Bucket,
    grid: Bucket,
    rebalance_every_weeks: int = 4,
) -> tuple[Bucket, Bucket, Bucket, list[dict]]:
    # Initial allocation
    mnq.equity = start_equity * mnq.allocation_pct
    perp.equity = start_equity * perp.allocation_pct
    grid.equity = start_equity * grid.allocation_pct
    timeline: list[dict] = []
    for w in range(1, weeks + 1):
        for b in (mnq, perp, grid):
            pnl, n = _week_pnl(
                b.equity,
                b.trades_per_week,
                b.expectancy_r,
                b.risk_per_trade_pct,
                b.win_rate,
            )
            b.equity = max(0.0, b.equity + pnl)
            b.trades_cum += n
            b.pnl_cum += pnl
            b.history.append({"week": w, "equity": round(b.equity, 2), "pnl": round(pnl, 2), "trades": n})
        total = mnq.equity + perp.equity + grid.equity
        # Rebalance periodically back to targets
        if rebalance_every_weeks > 0 and w % rebalance_every_weeks == 0:
            mnq.equity = total * mnq.allocation_pct
            perp.equity = total * perp.allocation_pct
            grid.equity = total * grid.allocation_pct
        timeline.append(
            {
                "week": w,
                "total": round(total, 2),
                "mnq": round(mnq.equity, 2),
                "perp": round(perp.equity, 2),
                "grid": round(grid.equity, 2),
            }
        )
    return mnq, perp, grid, timeline


def main() -> int:
    p = argparse.ArgumentParser(description="Apex Tier-A sweep allocation sim")
    p.add_argument(
        "--start", type=float, default=27_000.0, help="Starting equity post live-tiny promotion (default $27k)"
    )
    p.add_argument("--weeks", type=int, default=12)
    p.add_argument("--rebalance-weeks", type=int, default=4, help="Rebalance to 60/30/10 every N weeks (0 = never)")

    # Allocation weights
    p.add_argument("--mnq-alloc", type=float, default=0.60)
    p.add_argument("--perp-alloc", type=float, default=0.30)
    p.add_argument("--grid-alloc", type=float, default=0.10)

    # Per-bucket params (trades/week, win_rate, expectancy_r, risk_pct)
    p.add_argument("--mnq-tpw", type=float, default=30)
    p.add_argument("--mnq-wr", type=float, default=0.59)
    p.add_argument("--mnq-exp", type=float, default=0.47)
    p.add_argument("--mnq-risk", type=float, default=0.01)

    p.add_argument("--perp-tpw", type=float, default=42)
    p.add_argument("--perp-wr", type=float, default=0.50)
    p.add_argument("--perp-exp", type=float, default=0.30)
    p.add_argument("--perp-risk", type=float, default=0.008)

    p.add_argument("--grid-tpw", type=float, default=40)
    p.add_argument("--grid-wr", type=float, default=0.46)
    p.add_argument("--grid-exp", type=float, default=0.15)
    p.add_argument("--grid-risk", type=float, default=0.005)

    p.add_argument("--out-dir", type=Path, default=ROOT / "docs")
    args = p.parse_args()

    alloc_sum = args.mnq_alloc + args.perp_alloc + args.grid_alloc
    if not math.isclose(alloc_sum, 1.0, rel_tol=1e-3):
        print(f"Allocations must sum to 1.0 (got {alloc_sum})", file=sys.stderr)
        return 2

    mnq = Bucket(
        name="mnq_compounder",
        allocation_pct=args.mnq_alloc,
        trades_per_week=args.mnq_tpw,
        win_rate=args.mnq_wr,
        expectancy_r=args.mnq_exp,
        risk_per_trade_pct=args.mnq_risk,
    )
    perp = Bucket(
        name="perp_basket",
        allocation_pct=args.perp_alloc,
        trades_per_week=args.perp_tpw,
        win_rate=args.perp_wr,
        expectancy_r=args.perp_exp,
        risk_per_trade_pct=args.perp_risk,
    )
    grid = Bucket(
        name="grid_seed",
        allocation_pct=args.grid_alloc,
        trades_per_week=args.grid_tpw,
        win_rate=args.grid_wr,
        expectancy_r=args.grid_exp,
        risk_per_trade_pct=args.grid_risk,
    )

    print("EVOLUTIONARY TRADING ALGO -- Tier-A Sweep Simulation")
    print("=" * 100)
    print(
        f"Start: ${args.start:,.0f}   Weeks: {args.weeks}   "
        f"Allocations: MNQ={args.mnq_alloc * 100:.0f}% "
        f"PERP={args.perp_alloc * 100:.0f}% GRID={args.grid_alloc * 100:.0f}%",
    )
    print(
        f"Rebalance every {args.rebalance_weeks}w",
    )
    print("-" * 100)

    mnq, perp, grid, timeline = simulate(
        args.start,
        args.weeks,
        mnq,
        perp,
        grid,
        args.rebalance_weeks,
    )

    # Per-week table
    print(f"{'Wk':>3} {'TOTAL':>12} {'MNQ':>12} {'PERP':>12} {'GRID':>12}")
    for row in timeline:
        print(
            f"{row['week']:>3} ${row['total']:>11,.0f} "
            f"${row['mnq']:>11,.0f} ${row['perp']:>11,.0f} ${row['grid']:>11,.0f}",
        )
    final_total = mnq.equity + perp.equity + grid.equity
    ret_pct = 100.0 * (final_total - args.start) / max(args.start, 1e-9)
    print("-" * 100)
    print(
        f"Final: ${final_total:,.2f}   "
        f"Start: ${args.start:,.2f}   Return: {ret_pct:+.2f}%   "
        f"Trades: MNQ={mnq.trades_cum} PERP={perp.trades_cum} GRID={grid.trades_cum}",
    )
    print("=" * 100)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "kind": "apex_sweep_simulation",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "inputs": {
            "start_equity": args.start,
            "weeks": args.weeks,
            "rebalance_every_weeks": args.rebalance_weeks,
            "allocations": {
                "mnq_compounder": args.mnq_alloc,
                "perp_basket": args.perp_alloc,
                "grid_seed": args.grid_alloc,
            },
            "bucket_params": {
                "mnq_compounder": {
                    "tpw": args.mnq_tpw,
                    "wr": args.mnq_wr,
                    "exp_r": args.mnq_exp,
                    "risk": args.mnq_risk,
                },
                "perp_basket": {
                    "tpw": args.perp_tpw,
                    "wr": args.perp_wr,
                    "exp_r": args.perp_exp,
                    "risk": args.perp_risk,
                },
                "grid_seed": {"tpw": args.grid_tpw, "wr": args.grid_wr, "exp_r": args.grid_exp, "risk": args.grid_risk},
            },
        },
        "timeline": timeline,
        "buckets_final": {
            "mnq_compounder": {
                "equity": round(mnq.equity, 2),
                "trades": mnq.trades_cum,
                "pnl_cum": round(mnq.pnl_cum, 2),
            },
            "perp_basket": {
                "equity": round(perp.equity, 2),
                "trades": perp.trades_cum,
                "pnl_cum": round(perp.pnl_cum, 2),
            },
            "grid_seed": {
                "equity": round(grid.equity, 2),
                "trades": grid.trades_cum,
                "pnl_cum": round(grid.pnl_cum, 2),
            },
        },
        "total_final": round(final_total, 2),
        "total_return_pct": round(ret_pct, 4),
    }
    rp = args.out_dir / "sweep_sim_report.json"
    rp.write_text(json.dumps(report, indent=2))

    # Tearsheet
    lines: list[str] = []
    lines.append("EVOLUTIONARY TRADING ALGO -- Sweep Sim Tearsheet")
    lines.append("=" * 100)
    lines.append(
        f"Start: ${args.start:,.0f}   Weeks: {args.weeks}   "
        f"Allocations: MNQ={args.mnq_alloc * 100:.0f}% "
        f"PERP={args.perp_alloc * 100:.0f}% GRID={args.grid_alloc * 100:.0f}%   "
        f"Rebalance: {args.rebalance_weeks}w",
    )
    lines.append("-" * 100)
    lines.append(f"{'Wk':>3} {'TOTAL':>12} {'MNQ':>12} {'PERP':>12} {'GRID':>12}")
    for row in timeline:
        lines.append(
            f"{row['week']:>3} ${row['total']:>11,.0f} "
            f"${row['mnq']:>11,.0f} ${row['perp']:>11,.0f} ${row['grid']:>11,.0f}",
        )
    lines.append("-" * 100)
    lines.append(
        f"Final: ${final_total:,.2f}  "
        f"Return: {ret_pct:+.2f}%  "
        f"Trades: MNQ={mnq.trades_cum} PERP={perp.trades_cum} GRID={grid.trades_cum}",
    )
    lines.append("=" * 100)
    tp = args.out_dir / "sweep_sim_tearsheet.txt"
    tp.write_text("\n".join(lines) + "\n")

    print(f"Report:    {rp}")
    print(f"Tearsheet: {tp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
