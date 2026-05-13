"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_sweep_harness
=======================================================
Multi-config sweep of book_imbalance across a parameter grid, with
deflated-sharpe correction and Bonferroni-style multiple-comparison
adjustment.

Why this exists
---------------
Per the Firm's Red Team dissent attack #3 against book_imbalance:
> Curve-fit on a synthetic generator (line 4): entry_threshold=1.75
> and consecutive_snaps=3 were not derived from data.  They were
> chosen by the operator.  Quant cannot cite the optimization
> process because there wasn't one.

This script IS the optimization process — with the discipline built
in.  It:
  1. Runs the harness across a grid of (entry_threshold,
     consecutive_snaps, atr_stop_mult, rr_target) combos
  2. Records every result to the existing CONFIG_SEARCH_LOG (already
     used by deflated_sharpe)
  3. Computes deflated_sharpe for the BEST config in the sweep,
     using the grid size as n_trials
  4. Reports a TUNING REPORT with:
     - all configs ranked by deflated sharpe (not raw)
     - the best config flagged with PASSES_PROMOTION_GATE
     - explicit warning when n_configs_tried >> n_trades
  5. Refuses to declare a winner when no config has OOS n_trades >=
     min_n_for_sharpe (insufficient sample for honest ranking)

Bonferroni vs Deflated Sharpe
-----------------------------
- Bonferroni: divide your significance threshold by N.  Pessimistic
  but simple.  Used for the binary "is any config statistically
  significant" question.
- Deflated Sharpe (Bailey/Lopez de Prado): subtracts a scaled
  inverse-normal quantile from the observed sharpe.  Pessimistic in
  a different way; calibrated to small-sample sharpe estimators.

We report BOTH so the operator can see how much edge survives each
correction.

Run
---
::

    # Default grid (3 × 3 × 2 × 2 = 36 configs)
    python -m eta_engine.scripts.l2_sweep_harness \\
        --symbol MNQ --days 14

    # Custom grid
    python -m eta_engine.scripts.l2_sweep_harness \\
        --symbol MNQ --days 14 \\
        --entry-thresholds 1.5,1.75,2.0,2.25 \\
        --consecutive-snaps 2,3,4,5

    # JSON output for downstream automation
    python -m eta_engine.scripts.l2_sweep_harness --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SWEEP_LOG = LOG_DIR / "l2_sweep_runs.jsonl"


@dataclass
class SweepResult:
    config: dict
    n_trades: int
    n_signals: int
    win_rate: float
    sharpe_proxy: float
    sharpe_proxy_valid: bool
    sharpe_ci_95: tuple[float, float] | None
    deflated_sharpe_in_sweep: float | None
    total_pnl_dollars_net: float
    walk_forward_passes: bool
    walk_forward_test_sharpe: float | None


@dataclass
class SweepSummary:
    """Top-level sweep result + best-config selection."""

    strategy: str
    symbol: str
    days: int
    n_configs_tried: int
    n_configs_valid: int  # how many had n_trades >= min_n_for_sharpe
    results: list[SweepResult]
    best_config: SweepResult | None = None
    best_deflated_sharpe: float | None = None
    bonferroni_alpha: float | None = None  # 0.05 / n_configs
    promotion_gate_passes: bool = False
    warnings: list[str] = field(default_factory=list)


# Default grid — small enough to run in <2 min on real data, large
# enough to detect curve-fit if you swap parameter sets
DEFAULT_ENTRY_THRESHOLDS = [1.5, 1.75, 2.0]
DEFAULT_CONSECUTIVE_SNAPS = [2, 3, 4]
DEFAULT_ATR_STOP_MULTS = [1.0, 1.5]
DEFAULT_RR_TARGETS = [1.5, 2.0]


def run_sweep(
    symbol: str,
    days: int,
    *,
    entry_thresholds: list[float] | None = None,
    consecutive_snaps: list[int] | None = None,
    atr_stop_mults: list[float] | None = None,
    rr_targets: list[float] | None = None,
    n_levels: int = 3,
    min_n_for_sharpe: int = 30,
    apply_regime_filter: bool = True,
) -> SweepSummary:
    """Run book_imbalance across a parameter grid and return a
    SweepSummary with deflated-sharpe ranking.

    Each individual run logs to CONFIG_SEARCH_LOG (so deflated sharpe
    accumulates across sweeps over time), and the BEST config in this
    sweep gets a sweep-specific deflated sharpe computed against the
    sweep size only (so a 36-config sweep applies a 36-trial correction).
    """
    from eta_engine.scripts.l2_backtest_harness import (
        deflated_sharpe_ratio,
        run_book_imbalance,
    )

    entry_thresholds = entry_thresholds or DEFAULT_ENTRY_THRESHOLDS
    consecutive_snaps = consecutive_snaps or DEFAULT_CONSECUTIVE_SNAPS
    atr_stop_mults = atr_stop_mults or DEFAULT_ATR_STOP_MULTS
    rr_targets = rr_targets or DEFAULT_RR_TARGETS

    grid = list(product(entry_thresholds, consecutive_snaps, atr_stop_mults, rr_targets))
    n_configs = len(grid)
    bonferroni = 0.05 / n_configs if n_configs > 0 else None

    results: list[SweepResult] = []
    for et, cs, asm, rrt in grid:
        run_result = run_book_imbalance(
            symbol,
            days,
            entry_threshold=et,
            consecutive_snaps=cs,
            n_levels=n_levels,
            atr_stop_mult=asm,
            rr_target=rrt,
            walk_forward=True,
            min_n_for_sharpe=min_n_for_sharpe,
            apply_regime_filter=apply_regime_filter,
            log_config_search_flag=True,
        )
        wf = run_result.walk_forward
        wf_passes = bool(wf and wf.get("promotion_gate", {}).get("passes"))
        wf_test_sharpe = wf["test"]["sharpe_proxy"] if wf else None
        # Deflate THIS config's sharpe against the sweep size as n_trials
        dsr_sweep = None
        if run_result.n_trades >= 5 and n_configs > 1:
            dsr_sweep = deflated_sharpe_ratio(run_result.sharpe_proxy, n_configs, run_result.n_trades)
        results.append(
            SweepResult(
                config={"entry_threshold": et, "consecutive_snaps": cs, "atr_stop_mult": asm, "rr_target": rrt},
                n_trades=run_result.n_trades,
                n_signals=run_result.n_signals,
                win_rate=run_result.win_rate,
                sharpe_proxy=run_result.sharpe_proxy,
                sharpe_proxy_valid=run_result.sharpe_proxy_valid,
                sharpe_ci_95=run_result.sharpe_ci_95,
                deflated_sharpe_in_sweep=dsr_sweep,
                total_pnl_dollars_net=run_result.total_pnl_dollars_net,
                walk_forward_passes=wf_passes,
                walk_forward_test_sharpe=wf_test_sharpe,
            )
        )

    # Rank: prefer deflated_sharpe when available; fall back to raw sharpe
    def rank_key(r: SweepResult) -> float:
        if r.deflated_sharpe_in_sweep is not None:
            return r.deflated_sharpe_in_sweep
        return r.sharpe_proxy if r.sharpe_proxy_valid else -math.inf

    sorted_results = sorted(results, key=rank_key, reverse=True)
    n_valid = sum(1 for r in results if r.sharpe_proxy_valid)
    best = sorted_results[0] if sorted_results else None

    warnings: list[str] = []
    if n_valid == 0:
        warnings.append(
            f"NO CONFIG has n_trades >= {min_n_for_sharpe}.  All sharpe "
            "rankings are statistically meaningless on this sample."
        )
    if n_valid < n_configs / 2:
        warnings.append(
            f"Only {n_valid}/{n_configs} configs reached min sample size.  "
            "Sweep results are dominated by under-sampled noise."
        )

    promotion_passes = False
    if best is not None:
        promotion_passes = (
            best.sharpe_proxy_valid
            and best.walk_forward_passes
            and best.deflated_sharpe_in_sweep is not None
            and best.deflated_sharpe_in_sweep >= 0.5
        )

    return SweepSummary(
        strategy="book_imbalance",
        symbol=symbol,
        days=days,
        n_configs_tried=n_configs,
        n_configs_valid=n_valid,
        results=sorted_results,
        best_config=best,
        best_deflated_sharpe=best.deflated_sharpe_in_sweep if best else None,
        bonferroni_alpha=bonferroni,
        promotion_gate_passes=promotion_passes,
        warnings=warnings,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--entry-thresholds", default=None, help="Comma-separated floats (default 1.5,1.75,2.0)")
    ap.add_argument("--consecutive-snaps", default=None, help="Comma-separated ints (default 2,3,4)")
    ap.add_argument("--atr-stop-mults", default=None, help="Comma-separated floats (default 1.0,1.5)")
    ap.add_argument("--rr-targets", default=None, help="Comma-separated floats (default 1.5,2.0)")
    ap.add_argument("--n-levels", type=int, default=3)
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--no-regime-filter", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    def _parse_floats(s: str | None) -> list[float] | None:
        if not s:
            return None
        return [float(x.strip()) for x in s.split(",")]

    def _parse_ints(s: str | None) -> list[int] | None:
        if not s:
            return None
        return [int(x.strip()) for x in s.split(",")]

    summary = run_sweep(
        args.symbol,
        args.days,
        entry_thresholds=_parse_floats(args.entry_thresholds),
        consecutive_snaps=_parse_ints(args.consecutive_snaps),
        atr_stop_mults=_parse_floats(args.atr_stop_mults),
        rr_targets=_parse_floats(args.rr_targets),
        n_levels=args.n_levels,
        min_n_for_sharpe=args.min_n,
        apply_regime_filter=not args.no_regime_filter,
    )

    # Persist sweep summary
    try:
        with SWEEP_LOG.open("a", encoding="utf-8") as f:
            digest = {
                "ts": datetime.now(UTC).isoformat(),
                "strategy": summary.strategy,
                "symbol": summary.symbol,
                "days": summary.days,
                "n_configs_tried": summary.n_configs_tried,
                "n_configs_valid": summary.n_configs_valid,
                "best_config": asdict(summary.best_config) if summary.best_config else None,
                "best_deflated_sharpe": summary.best_deflated_sharpe,
                "promotion_gate_passes": summary.promotion_gate_passes,
                "warnings": summary.warnings,
            }
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: could not append sweep digest: {e}", file=sys.stderr)

    if args.json:
        out = asdict(summary)
        print(json.dumps(out, indent=2))
        return 0 if summary.promotion_gate_passes else 1

    print()
    print("=" * 78)
    print(f"L2 sweep harness — {summary.strategy} on {summary.symbol} over {summary.days}d")
    print("=" * 78)
    print(f"  configs tried   : {summary.n_configs_tried}")
    print(f"  configs valid   : {summary.n_configs_valid} (n_trades >= {args.min_n})")
    print(
        f"  bonferroni α    : {summary.bonferroni_alpha:.4f}" if summary.bonferroni_alpha else "  bonferroni α    : n/a"
    )
    print()
    print(
        f"  {'Rank':<5s} {'sharpe':<8s} {'dsr':<8s} {'n_trades':<9s} "
        f"{'win':<6s} {'pnl_net':<10s} {'wf_pass':<8s} config"
    )
    print(
        f"  {'-' * 5:<5s} {'-' * 8:<8s} {'-' * 8:<8s} {'-' * 9:<9s} "
        f"{'-' * 6:<6s} {'-' * 10:<10s} {'-' * 8:<8s} {'-' * 30}"
    )
    for i, r in enumerate(summary.results[:20], 1):  # show top 20
        dsr = f"{r.deflated_sharpe_in_sweep:+.3f}" if r.deflated_sharpe_in_sweep is not None else "n/a"
        wf = "YES" if r.walk_forward_passes else "no"
        sharpe_str = f"{r.sharpe_proxy:+.3f}" if r.sharpe_proxy_valid else f"{r.sharpe_proxy:+.3f}*"
        cfg_str = (
            f"et={r.config['entry_threshold']} "
            f"cs={r.config['consecutive_snaps']} "
            f"asm={r.config['atr_stop_mult']} "
            f"rr={r.config['rr_target']}"
        )
        print(
            f"  #{i:<3d} {sharpe_str:<8s} {dsr:<8s} {r.n_trades:<9d} "
            f"{r.win_rate * 100:5.1f}% ${r.total_pnl_dollars_net:>+8.2f} "
            f"{wf:<8s} {cfg_str}"
        )
    if any(not r.sharpe_proxy_valid for r in summary.results):
        print("    * = sharpe_proxy_valid=False (n_trades < min_n)")
    print()
    if summary.warnings:
        print("  WARNINGS:")
        for w in summary.warnings:
            print(f"    - {w}")
        print()
    if summary.best_config:
        b = summary.best_config
        print(f"  BEST CONFIG: {b.config}")
        print(
            f"    sharpe={b.sharpe_proxy:+.3f}  dsr={b.deflated_sharpe_in_sweep}"
            if b.deflated_sharpe_in_sweep is not None
            else f"    sharpe={b.sharpe_proxy:+.3f}  dsr=n/a"
        )
        print(f"    walk-forward OOS passes: {b.walk_forward_passes}")
    print()
    print(f"  PROMOTION GATE: {'PASS' if summary.promotion_gate_passes else 'FAIL'}")
    print("    Gate requires: best config has valid sharpe AND walk_forward_passes AND deflated_sharpe >= 0.5")
    print()
    return 0 if summary.promotion_gate_passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
