"""
EVOLUTIONARY TRADING ALGO  //  scripts.fleet_strategy_optimizer
=================================================================
Cross-bot strategy hunt: for every bot in the fleet, sweep a
hand-curated set of candidate strategies + parameter grids and
find the one(s) that pass the strict walk-forward gate.

Why this exists
---------------
The user's repeated directive: *"different things move different
prices — different bots need different strategies."* This script
operationalises that. Per-bot, the candidate set is pre-narrowed
based on what's known to work for that market structure:

* Index futures (MNQ/NQ 5m): RTH-anchored ORB family.
* Index futures daily (NQ D): DRB (prior-day range break).
* BTC 1h (trend-prone, momentum-friendly): crypto_orb at
  longer ranges, crypto_trend, crypto_regime_trend.
* ETH 1h (oscillating crab regime more often than BTC):
  crypto_meanrev (Bollinger+RSI) AND crypto_orb at tighter
  stops.
* SOL 1h (high-beta BTC proxy): crypto_orb at wider stops,
  crypto_trend with BTC-correlation alignment.
* Crypto daily (crypto_seed): crypto_trend long-only, DCA-
  style accumulator on regime context.
* Grid: GridTradingStrategy on BTC 1h as a baseline mean-
  reversion ladder.

The grids are intentionally small (8-32 cells per bot) so the
whole sweep finishes in a few minutes. Larger grids invite
p-hacking; we stay narrow and let walk-forward do the
out-of-sample selection.

Output
------
* stdout — per-bot progress + top cells
* ``docs/research_log/fleet_optimization_<ts>.md`` — per-bot
  ranked tables, summary of PASS configs, and the proposed
  registry edits to lock in winners.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# ---------------------------------------------------------------------------
# Per-bot candidate spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One (strategy_kind, config) pair to evaluate."""

    kind: str  # registers strategy dispatch in run_research_grid factory
    label: str  # human-friendly tag, shows in output table
    cfg: dict[str, Any]  # passed via cell.extras under "<kind>_config"


@dataclass(frozen=True)
class BotPlan:
    """All candidates to try for one bot."""

    bot_id: str
    symbol: str
    timeframe: str
    window_days: int
    step_days: int
    min_trades_per_window: int
    candidates: tuple[Candidate, ...]


def _orb_grid() -> tuple[Candidate, ...]:
    """Index-futures ORB grid (5m). Hand-curated around the 2026-04-27 winner."""
    cells = []
    for rm, rr, asm in product((10, 15, 30), (1.5, 2.0, 2.5), (1.5, 2.0, 2.5)):
        cells.append(
            Candidate(
                kind="orb",
                label=f"r{rm}/atr{asm}/rr{rr}",
                cfg={"range_minutes": rm, "atr_stop_mult": asm, "rr_target": rr},
            ),
        )
    return tuple(cells)


def _crypto_orb_grid() -> tuple[Candidate, ...]:
    """Crypto-ORB grid (UTC-anchored, 1h)."""
    cells = []
    for rm, asm, rr in product((60, 120, 240), (2.0, 2.5, 3.0), (1.5, 2.0, 2.5)):
        cells.append(
            Candidate(
                kind="crypto_orb",
                label=f"corb r{rm}/atr{asm}/rr{rr}",
                cfg={"range_minutes": rm, "atr_stop_mult": asm, "rr_target": rr},
            ),
        )
    return tuple(cells)


def _crypto_meanrev_grid() -> tuple[Candidate, ...]:
    """Bollinger+RSI mean-reversion grid for chop-prone 1h crypto."""
    cells = []
    for bb_mult, rsi_lo, rsi_hi in product((1.5, 2.0, 2.5), (25.0, 30.0), (70.0, 75.0)):
        cells.append(
            Candidate(
                kind="crypto_meanrev",
                label=f"mr bb{bb_mult}/rsi{int(rsi_lo)}-{int(rsi_hi)}",
                cfg={
                    "bb_stddev_mult": bb_mult,
                    "rsi_oversold": rsi_lo,
                    "rsi_overbought": rsi_hi,
                },
            ),
        )
    return tuple(cells)


def _crypto_trend_grid() -> tuple[Candidate, ...]:
    """EMA-crossover + HTF bias trend strategy. For prolonged-direction bots."""
    # CryptoTrendConfig fields vary; the safe-kwargs filter drops unknowns.
    cells = []
    for fast, slow in ((9, 21), (12, 26), (20, 50)):
        cells.append(
            Candidate(
                kind="crypto_trend",
                label=f"trend ema{fast}/{slow}",
                cfg={"fast_ema": fast, "slow_ema": slow},
            ),
        )
    return tuple(cells)


def _drb_grid() -> tuple[Candidate, ...]:
    """Daily Range Breakout — for the 27y NQ daily tape."""
    cells = []
    for rr, asm in product((1.5, 2.0, 2.5), (1.0, 1.5, 2.0)):
        cells.append(
            Candidate(
                kind="drb",
                label=f"drb atr{asm}/rr{rr}",
                cfg={"atr_stop_mult": asm, "rr_target": rr},
            ),
        )
    return tuple(cells)


def _grid_grid() -> tuple[Candidate, ...]:
    """Grid-trading ladder. Tunes spacing + n_levels."""
    cells = []
    for spacing_pct, n_levels in product((0.005, 0.01, 0.015), (4, 6)):
        cells.append(
            Candidate(
                kind="grid",
                label=f"grid sp{spacing_pct}/lvl{n_levels}",
                cfg={"grid_spacing_pct": spacing_pct, "n_levels": n_levels},
            ),
        )
    return tuple(cells)


def _build_plans() -> list[BotPlan]:
    """Per-bot plans. Pre-narrowed candidate sets per market structure."""
    plans: list[BotPlan] = []

    # Index futures intraday — ORB family is the proven baseline.
    for bot, symbol in (("mnq_futures", "MNQ1"), ("nq_futures", "NQ1")):
        plans.append(
            BotPlan(
                bot_id=bot,
                symbol=symbol,
                timeframe="5m",
                window_days=60,
                step_days=30,
                min_trades_per_window=3,
                candidates=_orb_grid(),
            ),
        )

    # NQ daily — DRB has the right-shape for the 27y daily tape.
    plans.append(
        BotPlan(
            bot_id="nq_daily_drb",
            symbol="NQ1",
            timeframe="D",
            window_days=365,
            step_days=180,
            min_trades_per_window=5,
            candidates=_drb_grid(),
        ),
    )

    # BTC 1h — crypto_orb known to work; trend + meanrev as diversifiers.
    plans.append(
        BotPlan(
            bot_id="btc_hybrid",
            symbol="BTC",
            timeframe="1h",
            window_days=90,
            step_days=30,
            min_trades_per_window=3,
            candidates=_crypto_orb_grid() + _crypto_trend_grid() + _crypto_meanrev_grid(),
        ),
    )

    # ETH 1h — crypto_orb didn't work IS-positively; meanrev is the
    # natural inverse trade for chop-prone regimes.
    plans.append(
        BotPlan(
            bot_id="eth_perp",
            symbol="ETH",
            timeframe="1h",
            window_days=90,
            step_days=30,
            min_trades_per_window=3,
            candidates=_crypto_meanrev_grid() + _crypto_orb_grid() + _crypto_trend_grid(),
        ),
    )

    # SOL 1h — high-beta BTC proxy. Wider-stop crypto_orb + trend.
    plans.append(
        BotPlan(
            bot_id="sol_perp",
            symbol="SOL",
            timeframe="1h",
            window_days=90,
            step_days=30,
            min_trades_per_window=3,
            candidates=_crypto_orb_grid() + _crypto_trend_grid(),
        ),
    )

    # crypto_seed — daily DCA accumulator. Trend on daily fits.
    plans.append(
        BotPlan(
            bot_id="crypto_seed",
            symbol="BTC",
            timeframe="D",
            window_days=365,
            step_days=180,
            min_trades_per_window=3,
            candidates=_crypto_trend_grid() + _drb_grid(),
        ),
    )

    # Grid bot — GridTradingStrategy on BTC 1h. We don't have a separate
    # bot dir, but ranking grid configs alongside the others lets the
    # operator decide whether to register one.
    plans.append(
        BotPlan(
            bot_id="grid_bot__btc",
            symbol="BTC",
            timeframe="1h",
            window_days=90,
            step_days=30,
            min_trades_per_window=3,
            candidates=_grid_grid(),
        ),
    )

    return plans


# ---------------------------------------------------------------------------
# Sweep runner
# ---------------------------------------------------------------------------


@dataclass
class CellRunResult:
    bot_id: str
    candidate: Candidate
    n_windows: int
    n_positive_oos: int
    agg_is_sharpe: float
    agg_oos_sharpe: float
    avg_oos_degradation: float
    fold_dsr_median: float
    fold_dsr_pass_fraction: float
    pass_gate: bool
    error: str = ""


def _run_one(  # type: ignore[no-untyped-def]  # noqa: ANN202
    plan: BotPlan, cand: Candidate,
):
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.scripts.run_research_grid import _build_crypto_strategy_factory

    ds = default_library().get(symbol=plan.symbol, timeframe=plan.timeframe)
    if ds is None:
        return CellRunResult(
            bot_id=plan.bot_id, candidate=cand, n_windows=0, n_positive_oos=0,
            agg_is_sharpe=0.0, agg_oos_sharpe=0.0, avg_oos_degradation=0.0,
            fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0, pass_gate=False,
            error=f"no data for {plan.symbol}/{plan.timeframe}",
        )
    bars = default_library().load_bars(ds)
    if not bars:
        return CellRunResult(
            bot_id=plan.bot_id, candidate=cand, n_windows=0, n_positive_oos=0,
            agg_is_sharpe=0.0, agg_oos_sharpe=0.0, avg_oos_degradation=0.0,
            fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0, pass_gate=False,
            error="empty bar list",
        )

    base_cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=plan.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=plan.window_days,
        step_days=plan.step_days,
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=plan.min_trades_per_window,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )

    extras = {f"{cand.kind}_config": dict(cand.cfg)}
    try:
        if cand.kind == "orb":
            from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy
            orb_cfg = ORBConfig(**cand.cfg)
            res = WalkForwardEngine().run(
                bars=bars, pipeline=FeaturePipeline.default(), config=wf,
                base_backtest_config=base_cfg, ctx_builder=lambda b, h: {},
                strategy_factory=lambda: ORBStrategy(orb_cfg),
            )
        elif cand.kind == "drb":
            from eta_engine.strategies.drb_strategy import DRBConfig, DRBStrategy
            drb_cfg = DRBConfig(**cand.cfg)
            res = WalkForwardEngine().run(
                bars=bars, pipeline=FeaturePipeline.default(), config=wf,
                base_backtest_config=base_cfg, ctx_builder=lambda b, h: {},
                strategy_factory=lambda: DRBStrategy(drb_cfg),
            )
        else:
            factory = _build_crypto_strategy_factory(cand.kind, extras)
            res = WalkForwardEngine().run(
                bars=bars, pipeline=FeaturePipeline.default(), config=wf,
                base_backtest_config=base_cfg, ctx_builder=lambda b, h: {},
                strategy_factory=factory,
            )
    except (ValueError, TypeError, KeyError) as exc:
        return CellRunResult(
            bot_id=plan.bot_id, candidate=cand, n_windows=0, n_positive_oos=0,
            agg_is_sharpe=0.0, agg_oos_sharpe=0.0, avg_oos_degradation=0.0,
            fold_dsr_median=0.0, fold_dsr_pass_fraction=0.0, pass_gate=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0.0) > 0)
    return CellRunResult(
        bot_id=plan.bot_id, candidate=cand, n_windows=len(res.windows),
        n_positive_oos=n_pos,
        agg_is_sharpe=res.aggregate_is_sharpe,
        agg_oos_sharpe=res.aggregate_oos_sharpe,
        avg_oos_degradation=res.oos_degradation_avg,
        fold_dsr_median=res.fold_dsr_median,
        fold_dsr_pass_fraction=res.fold_dsr_pass_fraction,
        pass_gate=res.pass_gate,
    )


def _rank(results: list[CellRunResult]) -> list[CellRunResult]:
    """Sort: PASS first, then by agg_oos_sharpe desc, then by deg asc."""
    return sorted(
        results,
        key=lambda r: (
            not r.pass_gate,
            -r.agg_oos_sharpe,
            r.avg_oos_degradation,
        ),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _bot_table(bot_id: str, ranked: list[CellRunResult], top_n: int = 6) -> list[str]:
    lines = [
        f"### {bot_id}",
        "",
        "| Verdict | Strategy | IS Sh | OOS Sh | Deg% | DSR med | DSR pass% | W | +OOS |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if not ranked:
        lines.append("| — | (no results) | | | | | | | |")
        return lines
    for r in ranked[:top_n]:
        if r.error:
            lines.append(
                f"| ERR | {r.candidate.kind}: {r.candidate.label} | | | | | | | |  "
                f"_(error: {r.error})_",
            )
            continue
        verdict = "**PASS**" if r.pass_gate else "FAIL"
        lines.append(
            f"| {verdict} | {r.candidate.kind}: {r.candidate.label} | "
            f"{r.agg_is_sharpe:+.3f} | {r.agg_oos_sharpe:+.3f} | "
            f"{r.avg_oos_degradation * 100:.1f} | "
            f"{r.fold_dsr_median:.3f} | "
            f"{r.fold_dsr_pass_fraction * 100:.1f} | "
            f"{r.n_windows} | {r.n_positive_oos} |",
        )
    lines.append("")
    return lines


def _summary_section(per_bot: dict[str, list[CellRunResult]]) -> list[str]:
    lines = [
        "## Summary — fleet PASS map",
        "",
        "| Bot | Best verdict | Best strategy | Best OOS Sh | # PASS configs |",
        "|---|---|---|---:|---:|",
    ]
    for bot_id, results in per_bot.items():
        if not results:
            lines.append(f"| {bot_id} | — | (no results) | | 0 |")
            continue
        ranked = _rank(results)
        best = ranked[0]
        n_pass = sum(1 for r in results if r.pass_gate)
        verdict = "**PASS**" if best.pass_gate else "FAIL"
        lines.append(
            f"| {bot_id} | {verdict} | "
            f"{best.candidate.kind}: {best.candidate.label} | "
            f"{best.agg_oos_sharpe:+.3f} | {n_pass} |",
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(prog="fleet_strategy_optimizer")
    p.add_argument(
        "--only-bot", default=None,
        help="restrict to one bot_id (e.g. eth_perp); default: all",
    )
    p.add_argument(
        "--out-dir", type=Path, default=ROOT / "docs" / "research_log",
    )
    args = p.parse_args()

    plans = _build_plans()
    if args.only_bot:
        plans = [p for p in plans if p.bot_id == args.only_bot]
        if not plans:
            print(f"unknown bot_id: {args.only_bot!r}")
            return 1

    total_cells = sum(len(p.candidates) for p in plans)
    print(
        f"[fleet_optimizer] {len(plans)} bots, {total_cells} total cells\n",
    )

    per_bot: dict[str, list[CellRunResult]] = {}
    for plan in plans:
        print(
            f"== {plan.bot_id} ({plan.symbol}/{plan.timeframe}) — "
            f"{len(plan.candidates)} candidates ==",
        )
        results: list[CellRunResult] = []
        for i, cand in enumerate(plan.candidates):
            r = _run_one(plan, cand)
            results.append(r)
            if r.error:
                tag = f"ERR  ({r.error[:40]})"
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
                f"OOS={best.agg_oos_sharpe:+.2f} "
                f"({n_pass} passing config{'s' if n_pass > 1 else ''})",
            )
        else:
            best = ranked[0]
            print(
                f"  -> NO PASS; closest fail: "
                f"{best.candidate.kind}:{best.candidate.label} "
                f"OOS={best.agg_oos_sharpe:+.2f}",
            )
        print()

    # Markdown report
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    md_path = args.out_dir / f"fleet_optimization_{stamp}.md"
    lines = [
        f"# Fleet Strategy Optimization — {datetime.now(UTC).isoformat()}",
        "",
        f"_Bots: {len(plans)}_  _Total cells: {total_cells}_",
        "",
        *_summary_section(per_bot),
        "## Per-bot ranked tables (top 6 each)",
        "",
    ]
    for bot_id, results in per_bot.items():
        lines.extend(_bot_table(bot_id, _rank(results)))
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[fleet_optimizer] wrote report to {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
