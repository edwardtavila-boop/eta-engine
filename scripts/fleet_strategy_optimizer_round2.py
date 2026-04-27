"""
EVOLUTIONARY TRADING ALGO  //  scripts.fleet_strategy_optimizer_round2
======================================================================
Round-2 deeper sweeps for bots that didn't PASS the strict gate in
the first fleet optimization run.

Per-bot candidate sets are tuned to address the specific failure mode
observed in round 1:

* **btc_hybrid** — round 1 was on the new 5y tape with 90d/30d windows
  (57 windows). Per-fold DSR was noisy (49.1pct top cell). Round 2
  scales window_days to 365 / step_days 90 (~17 windows of ~365d each)
  to give each fold enough trades for the DSR to stabilize. Also tries
  crypto_orb at extreme atr_stop_mult values (3.5, 4.0) to absorb
  multi-day BTC volatility regimes.
* **nq_daily_drb** — round 1 had agg OOS Sharpe +9.05 (huge!) but
  DSR pass-fraction only 39.6pct. The issue is per-fold variance, not
  signal quality. Round 2 tries longer windows (730d / 365d steps)
  to give each fold more trades, and adds DRB variants with tighter
  atr stops to reduce per-fold tail risk.
* **sol_perp** — SOL is high-beta BTC. Round 1's best was crypto_orb
  +2.50 OOS but IS-negative. Round 2 broadens to crypto_meanrev
  (Bollinger+RSI fits SOL's chop better) AND wider crypto_orb stops.
* **crypto_seed** — daily DCA accumulator. Round 1's crypto_trend
  failed. Round 2 tries crypto_orb on D timeframe (daily breakouts)
  and DRB with longer windows.
* **grid_bot** — round 1's spacing was off (0.5pct - 1.5pct on BTC 1h
  is too coarse). Round 2 sweeps tighter spacings (0.1pct - 0.4pct)
  and adds the no-trend-filter variant for pure mean-reversion.

Same output shape as round 1: per-bot ranked tables + summary.
Output:: ``docs/research_log/fleet_optimization_round2_<ts>.md``
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from eta_engine.scripts.fleet_strategy_optimizer import (  # noqa: E402
    BotPlan,
    Candidate,
    CellRunResult,
    _bot_table,
    _rank,
    _run_one,
    _summary_section,
)

# ---------------------------------------------------------------------------
# Round-2 per-bot grids
# ---------------------------------------------------------------------------


def _btc_round2() -> tuple[Candidate, ...]:
    """Wider crypto_orb on 5y tape + extreme stops."""
    cells: list[Candidate] = []
    # Standard sweep with longer ranges + wider stops
    for rm, asm, rr in product((120, 240, 360, 480), (2.5, 3.0, 3.5, 4.0), (1.5, 2.0, 2.5)):
        cells.append(
            Candidate(
                kind="crypto_orb",
                label=f"corb r{rm}/atr{asm}/rr{rr}",
                cfg={"range_minutes": rm, "atr_stop_mult": asm, "rr_target": rr},
            ),
        )
    return tuple(cells)


def _nq_drb_round2() -> tuple[Candidate, ...]:
    """Tighter atr stops + longer rr_target on 27y NQ daily."""
    cells = []
    for asm, rr in product((0.5, 0.75, 1.0, 1.25, 1.5), (1.5, 2.0, 2.5, 3.0)):
        cells.append(
            Candidate(
                kind="drb",
                label=f"drb atr{asm}/rr{rr}",
                cfg={"atr_stop_mult": asm, "rr_target": rr},
            ),
        )
    return tuple(cells)


def _sol_round2() -> tuple[Candidate, ...]:
    """SOL's chop calls for mean-reversion + wider crypto_orb."""
    cells: list[Candidate] = []
    # Bollinger+RSI mean-reversion: wider parameter ranges
    for bb, rsi_lo, rsi_hi in product(
        (1.5, 2.0, 2.5, 3.0), (20.0, 25.0, 30.0), (70.0, 75.0, 80.0),
    ):
        cells.append(
            Candidate(
                kind="crypto_meanrev",
                label=f"mr bb{bb}/rsi{int(rsi_lo)}-{int(rsi_hi)}",
                cfg={
                    "bb_stddev_mult": bb,
                    "rsi_oversold": rsi_lo,
                    "rsi_overbought": rsi_hi,
                },
            ),
        )
    # Crypto_orb with wider stops to absorb SOL's volatility
    for rm, asm, rr in product((60, 120, 240), (3.0, 3.5, 4.0), (2.0, 2.5)):
        cells.append(
            Candidate(
                kind="crypto_orb",
                label=f"corb r{rm}/atr{asm}/rr{rr}",
                cfg={"range_minutes": rm, "atr_stop_mult": asm, "rr_target": rr},
            ),
        )
    return tuple(cells)


def _crypto_seed_round2() -> tuple[Candidate, ...]:
    """Daily timeframe DCA — try crypto_orb on D and broader DRB."""
    cells: list[Candidate] = []
    # Daily ORB on BTC D doesn't really work (1 bar = 1 day, no
    # intraday range), but DRB on daily IS the right shape.
    for asm, rr in product((0.5, 1.0, 1.5, 2.0), (1.5, 2.0, 2.5)):
        cells.append(
            Candidate(
                kind="drb",
                label=f"drb atr{asm}/rr{rr}",
                cfg={"atr_stop_mult": asm, "rr_target": rr},
            ),
        )
    # Crypto_trend at multiple EMA combos
    for fast, slow in ((9, 21), (12, 26), (20, 50), (50, 200)):
        cells.append(
            Candidate(
                kind="crypto_trend",
                label=f"trend ema{fast}/{slow}",
                cfg={"fast_ema": fast, "slow_ema": slow},
            ),
        )
    return tuple(cells)


def _grid_round2() -> tuple[Candidate, ...]:
    """Tighter spacing for BTC 1h's typical 0.1-0.3pct micro-moves."""
    cells = []
    for spacing_pct, n_levels in product(
        (0.001, 0.002, 0.003, 0.005), (4, 6, 8),
    ):
        cells.append(
            Candidate(
                kind="grid",
                label=f"grid sp{spacing_pct}/lvl{n_levels}",
                cfg={"grid_spacing_pct": spacing_pct, "n_levels": n_levels},
            ),
        )
    # Trend-filter off — pure ladder
    for spacing_pct in (0.002, 0.005):
        cells.append(
            Candidate(
                kind="grid",
                label=f"grid sp{spacing_pct}/lvl6/no_trend",
                cfg={
                    "grid_spacing_pct": spacing_pct,
                    "n_levels": 6,
                    "trend_filter": False,
                },
            ),
        )
    return tuple(cells)


def _build_round2_plans() -> list[BotPlan]:
    return [
        # BTC 5y: longer windows, fewer of them, more trades per fold
        BotPlan(
            bot_id="btc_hybrid",
            symbol="BTC",
            timeframe="1h",
            window_days=365,
            step_days=90,
            min_trades_per_window=10,
            candidates=_btc_round2(),
        ),
        # NQ 27y daily: longer windows + DSR-tightening grid
        BotPlan(
            bot_id="nq_daily_drb",
            symbol="NQ1",
            timeframe="D",
            window_days=730,
            step_days=365,
            min_trades_per_window=10,
            candidates=_nq_drb_round2(),
        ),
        # SOL: mean-reversion + wider stops
        BotPlan(
            bot_id="sol_perp",
            symbol="SOL",
            timeframe="1h",
            window_days=90,
            step_days=30,
            min_trades_per_window=3,
            candidates=_sol_round2(),
        ),
        # crypto_seed: DRB + trend on daily
        BotPlan(
            bot_id="crypto_seed",
            symbol="BTC",
            timeframe="D",
            window_days=365,
            step_days=180,
            min_trades_per_window=3,
            candidates=_crypto_seed_round2(),
        ),
        # Grid: tighter spacings
        BotPlan(
            bot_id="grid_bot__btc",
            symbol="BTC",
            timeframe="1h",
            window_days=90,
            step_days=30,
            min_trades_per_window=3,
            candidates=_grid_round2(),
        ),
    ]


def main() -> int:
    p = argparse.ArgumentParser(prog="fleet_strategy_optimizer_round2")
    p.add_argument("--only-bot", default=None)
    p.add_argument(
        "--out-dir", type=Path, default=ROOT / "docs" / "research_log",
    )
    args = p.parse_args()

    plans = _build_round2_plans()
    if args.only_bot:
        plans = [pl for pl in plans if pl.bot_id == args.only_bot]
        if not plans:
            print(f"unknown bot_id: {args.only_bot!r}")
            return 1

    total_cells = sum(len(p.candidates) for p in plans)
    print(
        f"[round2] {len(plans)} bots, {total_cells} total cells\n",
    )

    per_bot: dict[str, list[CellRunResult]] = {}
    for plan in plans:
        print(
            f"== {plan.bot_id} ({plan.symbol}/{plan.timeframe}, "
            f"win={plan.window_days}d/step={plan.step_days}d) — "
            f"{len(plan.candidates)} candidates ==",
        )
        results: list[CellRunResult] = []
        for i, cand in enumerate(plan.candidates):
            r = _run_one(plan, cand)
            results.append(r)
            if r.error:
                tag = f"ERR  ({r.error[:50]})"
            else:
                verdict = "PASS" if r.pass_gate else "fail"
                tag = (
                    f"{verdict:4} "
                    f"OOS={r.agg_oos_sharpe:+6.2f} "
                    f"IS={r.agg_is_sharpe:+6.2f} "
                    f"deg={r.avg_oos_degradation * 100:5.1f}% "
                    f"dsr_pass={r.fold_dsr_pass_fraction * 100:4.1f}%"
                )
            print(
                f"  [{i + 1:2d}/{len(plan.candidates)}] "
                f"{cand.kind}:{cand.label:30}  {tag}",
            )
        per_bot[plan.bot_id] = results
        ranked = _rank(results)
        n_pass = sum(1 for r in results if r.pass_gate)
        if n_pass:
            best = ranked[0]
            print(
                f"  -> winner: {best.candidate.kind}:{best.candidate.label} "
                f"OOS={best.agg_oos_sharpe:+.2f} ({n_pass} passing)",
            )
        else:
            best = ranked[0]
            print(
                f"  -> NO PASS; closest: "
                f"{best.candidate.kind}:{best.candidate.label} "
                f"OOS={best.agg_oos_sharpe:+.2f}",
            )
        print()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    md_path = args.out_dir / f"fleet_optimization_round2_{stamp}.md"
    lines = [
        f"# Fleet Strategy Optimization — Round 2 — {datetime.now(UTC).isoformat()}",
        "",
        f"_Bots: {len(plans)}_  _Total cells: {total_cells}_",
        "",
        "Round-2 deeper sweeps targeted at the 5 bots that didn't",
        "PASS in round 1. Per-bot grids tuned to address the specific",
        "failure mode each bot exhibited.",
        "",
        *_summary_section(per_bot),
        "## Per-bot ranked tables (top 8 each)",
        "",
    ]
    for bot_id, results in per_bot.items():
        lines.extend(_bot_table(bot_id, _rank(results), top_n=8))
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[round2] wrote report to {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
