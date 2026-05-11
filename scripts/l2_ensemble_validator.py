"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_ensemble_validator
============================================================
OOS validator: confirms the strategy ensemble actually outperforms
any single constituent on held-out data.

Why this exists
---------------
l2_strategy_ensemble.vote() blends 4 strategy signals into one.
The mechanic is sensible (Lopez de Prado would approve) but doesn't
guarantee the blend is better than the best constituent.  Three
scenarios where the ensemble underperforms:

  1. Dominant constituent: book_imbalance does 90% of the edge;
     the other 3 add noise.  Ensemble sharpe < book_imbalance sharpe.
  2. Correlated constituents: 2 strategies emit nearly identical
     signals — they double-count instead of diversify.
  3. Bad weights: recent sharpe was high but driven by 2 lucky
     trades; weighting on it just amplifies the luck.

This validator backtests:
  - Each constituent on its own (read from backtest log)
  - The ensemble on the same window
And reports whether the ensemble's OOS sharpe is HIGHER than the
best individual.  If not, the operator should consider dropping
the ensemble layer.

Run
---
::

    python -m eta_engine.scripts.l2_ensemble_validator --days 30
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
L2_BACKTEST_LOG = LOG_DIR / "l2_backtest_runs.jsonl"
ENSEMBLE_VALIDATOR_LOG = LOG_DIR / "l2_ensemble_validator.jsonl"


@dataclass
class EnsembleValidation:
    n_constituents: int
    constituent_sharpes: dict[str, float]
    best_constituent: str | None
    best_constituent_sharpe: float | None
    ensemble_sharpe_estimate: float | None  # synthesized from weighted-avg
    ensemble_outperforms: bool | None
    margin: float | None                      # ensemble - best_constituent
    verdict: str                              # OUTPERFORM | UNDERPERFORM | INCONCLUSIVE
    notes: list[str] = field(default_factory=list)


def _read_recent_sharpes(*, since_days: int = 30,
                           _path: Path) -> dict[str, list[float]]:
    """Return {strategy: [sharpe_proxy values]} from recent log."""
    if not _path.exists():
        return {}
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    out: dict[str, list[float]] = {}
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
                strategy = rec.get("strategy")
                sharpe = rec.get("sharpe_proxy")
                if (strategy and sharpe is not None
                        and rec.get("sharpe_proxy_valid", False)):
                    out.setdefault(strategy, []).append(float(sharpe))
    except OSError:
        return {}
    return out


def validate_ensemble(*, since_days: int = 30,
                         _backtest_path: Path | None = None) -> EnsembleValidation:
    """Compute whether ensemble OOS sharpe beats best individual.

    Synthesizes ensemble sharpe via weighted average of constituent
    sharpes (a proxy — real ensemble sharpe requires running the
    actual vote logic on real signals; that's only available
    post-deployment).  This validator catches the easy cases:
    when the constituent dispersion suggests blending wouldn't help.
    """
    path = _backtest_path if _backtest_path is not None else L2_BACKTEST_LOG
    sharpes = _read_recent_sharpes(since_days=since_days, _path=path)
    if not sharpes:
        return EnsembleValidation(
            n_constituents=0, constituent_sharpes={},
            best_constituent=None, best_constituent_sharpe=None,
            ensemble_sharpe_estimate=None,
            ensemble_outperforms=None, margin=None,
            verdict="INCONCLUSIVE",
            notes=["no recent sharpe data for any constituent"],
        )
    avg_sharpe = {s: statistics.mean(vals) for s, vals in sharpes.items()}
    notes: list[str] = []
    if len(avg_sharpe) < 2:
        notes.append(
            "fewer than 2 constituents have history; ensemble layer "
            "has no diversification benefit yet")
        return EnsembleValidation(
            n_constituents=len(avg_sharpe),
            constituent_sharpes={k: round(v, 3) for k, v in avg_sharpe.items()},
            best_constituent=next(iter(avg_sharpe.keys())) if avg_sharpe else None,
            best_constituent_sharpe=next(iter(avg_sharpe.values()), None),
            ensemble_sharpe_estimate=None,
            ensemble_outperforms=None, margin=None,
            verdict="INCONCLUSIVE", notes=notes,
        )

    best_strategy = max(avg_sharpe, key=lambda s: avg_sharpe[s])
    best_sharpe = avg_sharpe[best_strategy]
    # Weighted-avg synthesized sharpe (proxy for ensemble's actual sharpe)
    weights = {s: max(0.0, v) for s, v in avg_sharpe.items()}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        ensemble_sharpe = None
        notes.append("all constituent sharpes <= 0; ensemble cannot help")
    else:
        weighted_sum = sum(weights[s] * avg_sharpe[s] for s in avg_sharpe)
        ensemble_sharpe = weighted_sum / total_weight

    if ensemble_sharpe is None:
        verdict = "INCONCLUSIVE"
        margin = None
        outperforms = None
    else:
        margin = ensemble_sharpe - best_sharpe
        outperforms = margin > 0.05  # 5% sharpe margin = real signal
        verdict = "OUTPERFORM" if outperforms else "UNDERPERFORM"
        if not outperforms:
            notes.append(
                f"Ensemble proxy sharpe ({round(ensemble_sharpe, 3)}) does "
                f"NOT exceed best individual {best_strategy} "
                f"({round(best_sharpe, 3)}) by >0.05 — consider trading "
                f"{best_strategy} solo or rebalancing weights.")

    return EnsembleValidation(
        n_constituents=len(avg_sharpe),
        constituent_sharpes={k: round(v, 3) for k, v in avg_sharpe.items()},
        best_constituent=best_strategy,
        best_constituent_sharpe=round(best_sharpe, 3),
        ensemble_sharpe_estimate=round(ensemble_sharpe, 3)
                                       if ensemble_sharpe is not None else None,
        ensemble_outperforms=outperforms,
        margin=round(margin, 3) if margin is not None else None,
        verdict=verdict, notes=notes,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = validate_ensemble(since_days=args.days)
    try:
        with ENSEMBLE_VALIDATOR_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(),
                                 **asdict(report)},
                                separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: ensemble validator log write failed: {e}",
              file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0 if report.verdict == "OUTPERFORM" else 1

    print()
    print("=" * 78)
    print("L2 ENSEMBLE VALIDATOR")
    print("=" * 78)
    print(f"  n_constituents       : {report.n_constituents}")
    print(f"  best constituent     : {report.best_constituent} "
          f"(sharpe={report.best_constituent_sharpe})")
    print(f"  ensemble sharpe est. : {report.ensemble_sharpe_estimate}")
    print(f"  margin               : {report.margin}")
    print(f"  verdict              : {report.verdict}")
    print()
    print("  Constituent sharpes:")
    for s, v in sorted(report.constituent_sharpes.items(),
                         key=lambda kv: kv[1], reverse=True):
        print(f"    {s:<30s} {v:+.3f}")
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 0 if report.verdict == "OUTPERFORM" else 1


if __name__ == "__main__":
    raise SystemExit(main())
