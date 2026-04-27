"""
EVOLUTIONARY TRADING ALGO  //  scripts.tier_b_param_sweep
=============================================
Parameter sweep across the 4 Tier-B bots (crypto_seed, eth_perp, sol_perp,
xrp_perp) to find configurations that clear the paper_phase_requirements
expectancy_R >= 0.30 gate on synthetic bars.

Sweeps:
  confluence_threshold  \u2208 {4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5}
  risk_per_trade_pct    \u2208 {0.005, 0.008, 0.010, 0.015}

For each (bot, conf_thr, risk_pct) cell it runs the existing harness single-bot
path and records the result. Top-1 configs per bot are emitted to
docs/tier_b_winning_configs.json for use by --overrides.

Usage:
    python -m eta_engine.scripts.tier_b_param_sweep [--weeks 4] [--seed 11]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


TIER_B_BOTS: tuple[str, ...] = ("crypto_seed", "eth_perp", "sol_perp", "xrp_perp")
CONFLUENCE_GRID: tuple[float, ...] = (4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5)
RISK_GRID: tuple[float, ...] = (0.005, 0.008, 0.010, 0.015)


@dataclass
class SweepCell:
    bot: str
    confluence_threshold: float
    risk_per_trade_pct: float
    n_trades: int
    win_rate: float
    expectancy_r: float
    max_dd_pct: float
    total_return_pct: float
    gate_pass: bool


def _run_cell(
    bot: str,
    conf_thr: float,
    risk_pct: float,
    weeks: int,
    seed: int,
) -> SweepCell:
    from eta_engine.scripts import paper_run_harness as prh

    base = dict(prh.BOT_PLAN[bot])
    base["confluence_threshold"] = float(conf_thr)
    base["risk_per_trade_pct"] = float(risk_pct)
    r = prh._run_one_bot(
        bot,
        base,
        weeks=weeks,
        seed=seed,
        reqs=prh.PaperPhaseRequirements(),
    )
    return SweepCell(
        bot=bot,
        confluence_threshold=conf_thr,
        risk_per_trade_pct=risk_pct,
        n_trades=r.n_trades,
        win_rate=round(r.win_rate, 4),
        expectancy_r=round(r.expectancy_r, 4),
        max_dd_pct=round(r.max_dd_pct, 4),
        total_return_pct=round(r.total_return_pct, 4),
        gate_pass=r.gate_pass,
    )


def _winner(cells: list[SweepCell]) -> SweepCell | None:
    """Pick the best gate-passing cell. Tie-break: highest expectancy_r,
    then lowest max_dd_pct."""
    passing = [c for c in cells if c.gate_pass]
    if passing:
        return sorted(
            passing,
            key=lambda c: (-c.expectancy_r, c.max_dd_pct),
        )[0]
    # No passers — return the one with the highest expectancy as "closest to passing"
    return sorted(cells, key=lambda c: -c.expectancy_r)[0] if cells else None


def main() -> int:
    p = argparse.ArgumentParser(description="Tier-B parameter sweep")
    p.add_argument("--weeks", type=int, default=4)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "docs",
    )
    args = p.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print("EVOLUTIONARY TRADING ALGO -- Tier-B Parameter Sweep")
    print("=" * 96)
    print(
        f"Weeks: {args.weeks}   Seed: {args.seed}   "
        f"Bots: {len(TIER_B_BOTS)}   Grid: {len(CONFLUENCE_GRID)}x{len(RISK_GRID)} = "
        f"{len(CONFLUENCE_GRID) * len(RISK_GRID)} cells/bot  "
        f"Total: {len(TIER_B_BOTS) * len(CONFLUENCE_GRID) * len(RISK_GRID)} runs",
    )
    print("-" * 96)

    all_cells: list[SweepCell] = []
    winners: dict[str, SweepCell] = {}
    for bot_i, bot in enumerate(TIER_B_BOTS):
        bot_cells: list[SweepCell] = []
        for conf in CONFLUENCE_GRID:
            for risk in RISK_GRID:
                cell = _run_cell(
                    bot,
                    conf,
                    risk,
                    weeks=args.weeks,
                    seed=args.seed + bot_i * 7,
                )
                bot_cells.append(cell)
                all_cells.append(cell)
        win = _winner(bot_cells)
        if win is not None:
            winners[bot] = win
            flag = "PASS" if win.gate_pass else "FAIL"
            print(
                f"[{bot:<12}] best: conf={win.confluence_threshold:>4.1f}  "
                f"risk={win.risk_per_trade_pct * 100:>4.2f}%  "
                f"trades={win.n_trades:>3}  "
                f"exp={win.expectancy_r:>+6.3f}R  "
                f"dd={win.max_dd_pct:>5.2f}%  "
                f"ret={win.total_return_pct:>+6.2f}%  "
                f"gate={flag}",
            )

    # Emit winning-configs JSON (for use as harness --overrides)
    configs: dict[str, dict] = {}
    for bot, w in winners.items():
        configs[bot] = {
            "confluence_threshold": w.confluence_threshold,
            "risk_per_trade_pct": w.risk_per_trade_pct,
            "expected_expectancy_r": w.expectancy_r,
            "expected_gate_pass": w.gate_pass,
        }
    winners_path = out_dir / "tier_b_winning_configs.json"
    winners_path.write_text(json.dumps(configs, indent=2))

    # Full sweep grid (for audit)
    full_path = out_dir / "tier_b_sweep_full.json"
    full_path.write_text(
        json.dumps([asdict(c) for c in all_cells], indent=2),
    )

    passers = sum(1 for w in winners.values() if w.gate_pass)
    print("-" * 96)
    print(
        f"Winners emitted: {winners_path}   ({passers}/{len(winners)} gate-pass)",
    )
    print(f"Full grid:       {full_path}   ({len(all_cells)} cells)")
    print("=" * 96)
    return 0 if passers == len(TIER_B_BOTS) else 2


if __name__ == "__main__":
    sys.exit(main())
