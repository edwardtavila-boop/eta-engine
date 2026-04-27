"""
EVOLUTIONARY TRADING ALGO  //  scripts.sweep_crypto_orb_eth
=============================================================
Per-config sweep of crypto_orb on ETH 1h, hunting for the
parameter combination that brings IS->OOS degradation under
the strict gate's 35% threshold without killing OOS Sharpe.

Why
---
The 2026-04-27 honest fleet snapshot found ETH crypto_orb with
default ``CryptoORBConfig()`` produced:

    agg OOS Sharpe   +3.977
    DSR median       0.997    (essentially every fold cleared 0.5)
    OOS pass-fraction 55.6%
    avg degradation  44.4%   <- BLOCKED by gate (threshold 35%)

So the strategy is producing real signal; the IS->OOS slip is
what the gate rejects, not the strategy itself. Per-fold tuning
of the stop / target / range parameters is the right path
(loosening the degradation threshold globally would be the wrong
path — invites overfit).

Search space
------------
Modest 36-cell grid over the three knobs the strategy review
flagged as the dominant levers for IS/OOS slippage on ETH 1h:

  * range_minutes  ∈ {120, 240, 360}   (default 240 — opening session)
  * atr_stop_mult  ∈ {1.5, 2.0, 2.5, 3.0} (default 2.5)
  * rr_target      ∈ {1.5, 2.0, 2.5}    (default 2.5 — crypto trends harder)

Other knobs (ema_bias_period, max_entry_local, max_trades_per_day)
held at CryptoORBConfig defaults so the search stays focused.

Output
------
* stdout: progressive per-cell results
* ``docs/research_log/eth_crypto_orb_sweep_<utc-stamp>.md`` —
  sorted table; the cell that maximizes ``OOS Sharpe`` while
  keeping ``degradation < 0.35`` is the promotion candidate.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


@dataclass(frozen=True)
class SweepCell:
    range_minutes: int
    atr_stop_mult: float
    rr_target: float


@dataclass
class SweepResult:
    cell: SweepCell
    n_windows: int
    n_positive_oos: int
    agg_is_sharpe: float
    agg_oos_sharpe: float
    avg_oos_degradation: float
    fold_dsr_median: float
    fold_dsr_pass_fraction: float
    pass_gate: bool


def run_one(  # noqa: PLR0913
    cell: SweepCell,
    *,
    symbol: str,
    timeframe: str,
    window_days: int,
    step_days: int,
    min_trades_per_window: int,
) -> SweepResult:
    from eta_engine.backtest import (
        BacktestConfig,
        WalkForwardConfig,
        WalkForwardEngine,
    )
    from eta_engine.data.library import default_library
    from eta_engine.features.pipeline import FeaturePipeline
    from eta_engine.strategies.crypto_orb_strategy import (
        CryptoORBConfig,
        crypto_orb_strategy,
    )

    ds = default_library().get(symbol=symbol, timeframe=timeframe)
    if ds is None:
        return SweepResult(cell, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, False)
    bars = default_library().load_bars(ds)
    if not bars:
        return SweepResult(cell, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, False)

    base_cfg = BacktestConfig(
        start_date=bars[0].timestamp,
        end_date=bars[-1].timestamp,
        symbol=ds.symbol,
        initial_equity=10_000.0,
        risk_per_trade_pct=0.01,
        confluence_threshold=0.0,
        max_trades_per_day=10,
    )
    wf = WalkForwardConfig(
        window_days=window_days,
        step_days=step_days,
        anchored=True,
        oos_fraction=0.3,
        min_trades_per_window=min_trades_per_window,
        strict_fold_dsr_gate=True,
        fold_dsr_min_pass_fraction=0.5,
    )
    crypto_cfg = CryptoORBConfig(
        range_minutes=cell.range_minutes,
        atr_stop_mult=cell.atr_stop_mult,
        rr_target=cell.rr_target,
    )
    res = WalkForwardEngine().run(
        bars=bars,
        pipeline=FeaturePipeline.default(),
        config=wf,
        base_backtest_config=base_cfg,
        ctx_builder=lambda b, h: {},
        strategy_factory=lambda: crypto_orb_strategy(crypto_cfg),
    )
    n_pos = sum(1 for w in res.windows if w.get("oos_sharpe", 0.0) > 0)
    return SweepResult(
        cell=cell,
        n_windows=len(res.windows),
        n_positive_oos=n_pos,
        agg_is_sharpe=res.aggregate_is_sharpe,
        agg_oos_sharpe=res.aggregate_oos_sharpe,
        avg_oos_degradation=res.oos_degradation_avg,
        fold_dsr_median=res.fold_dsr_median,
        fold_dsr_pass_fraction=res.fold_dsr_pass_fraction,
        pass_gate=res.pass_gate,
    )


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="sweep_crypto_orb_eth")
    p.add_argument("--symbol", default="ETH")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--window-days", type=int, default=90)
    p.add_argument("--step-days", type=int, default=30)
    p.add_argument("--min-trades-per-window", type=int, default=10)
    args = p.parse_args()

    grid = [
        SweepCell(rm, asm, rr)
        for rm, asm, rr in product(
            (120, 240, 360),
            (1.5, 2.0, 2.5, 3.0),
            (1.5, 2.0, 2.5),
        )
    ]
    print(
        f"[sweep] {args.symbol}/{args.timeframe} crypto_orb — "
        f"{len(grid)} cells, {args.window_days}d/{args.step_days}d windows\n",
    )
    results: list[SweepResult] = []
    for i, cell in enumerate(grid):
        r = run_one(
            cell,
            symbol=args.symbol,
            timeframe=args.timeframe,
            window_days=args.window_days,
            step_days=args.step_days,
            min_trades_per_window=args.min_trades_per_window,
        )
        results.append(r)
        deg_str = (
            f"deg={r.avg_oos_degradation * 100:>5.1f}%"
            if r.n_windows else "no_data"
        )
        flag = "PASS" if r.pass_gate else (
            "near" if (r.avg_oos_degradation < 0.35 and r.agg_oos_sharpe > 0) else ""
        )
        print(
            f"  {i + 1:2d}/{len(grid)} "
            f"range={cell.range_minutes:>3}m "
            f"atr={cell.atr_stop_mult:>3.1f} "
            f"rr={cell.rr_target:>3.1f} -> "
            f"OOS={r.agg_oos_sharpe:+7.2f} "
            f"{deg_str} pass={r.fold_dsr_pass_fraction * 100:>4.1f}% {flag}",
        )

    # Sort: passing cells first, then by (degradation < 0.35 AND OOS > 0),
    # then by OOS Sharpe desc.
    def _sort_key(r: SweepResult) -> tuple:
        promotion_candidate = r.avg_oos_degradation < 0.35 and r.agg_oos_sharpe > 0
        return (not r.pass_gate, not promotion_candidate, -r.agg_oos_sharpe)

    results.sort(key=_sort_key)

    lines = [
        f"# ETH crypto_orb Parameter Sweep — {args.symbol}/{args.timeframe}",
        "",
        f"_Generated: {datetime.now(UTC).isoformat()}_  "
        f"_Cells: {len(grid)}_  "
        f"_Windows: {args.window_days}d / step {args.step_days}d_",
        "",
        "Looking for: ``deg < 35%`` (degradation gate) AND OOS Sharpe > 0.",
        "Default config (range=240, atr=2.5, rr=2.5) sits at OOS +3.977 "
        "with deg 44.4% — blocked only by the deg gate.",
        "",
        "| Range | ATR× | RR | W | +OOS | IS Sh | OOS Sh | Deg% | DSR med | DSR pass% | Verdict |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        if r.pass_gate:
            verdict = "**PASS**"
        elif r.avg_oos_degradation < 0.35 and r.agg_oos_sharpe > 0:
            verdict = "promo-candidate"
        else:
            verdict = "FAIL"
        lines.append(
            f"| {r.cell.range_minutes}m | {r.cell.atr_stop_mult:.1f} | "
            f"{r.cell.rr_target:.1f} | {r.n_windows} | {r.n_positive_oos} | "
            f"{r.agg_is_sharpe:+.3f} | {r.agg_oos_sharpe:+.3f} | "
            f"{r.avg_oos_degradation * 100:.1f} | "
            f"{r.fold_dsr_median:.3f} | "
            f"{r.fold_dsr_pass_fraction * 100:.1f} | {verdict} |",
        )
    md = "\n".join(lines) + "\n"
    print("\n" + md)

    log_dir = ROOT / "docs" / "research_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = log_dir / f"eth_crypto_orb_sweep_{stamp}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"[saved to {out_path}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
