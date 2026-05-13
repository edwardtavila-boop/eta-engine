"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_cpcv
==============================================
Combinatorial Purged Cross-Validation (Lopez de Prado, 2018):
overfitting-resistant CV scheme that generates many train/test
splits while purging adjacent labels to avoid information leakage
between adjacent samples.

Why this exists
---------------
Walk-forward 70/30 (already in the harness) gives ONE out-of-sample
estimate.  K-fold CV gives K estimates but suffers leakage when
trades are time-correlated (typical in L2 strategies).  CPCV
generates MANY estimates:

  Given N folds, choose k of them as test → C(N, k) splits.
  For each split, the training set excludes:
    - The test folds themselves
    - A "purge zone" of P samples adjacent to each test fold edge
      (prevents look-ahead from data leakage)
    - An "embargo" of E samples AFTER each test fold
      (prevents the strategy state machine from learning the
       transition out of training data)

Output is a distribution of CV scores rather than a point estimate.
The standard deviation of that distribution is itself a metric: a
strategy whose IS sharpe is 1.5 across all splits is more robust
than one whose splits range from -1 to +3.

Reference
---------
Lopez de Prado, "Advances in Financial Machine Learning" (2018),
Chapter 7: "Cross-Validation in Finance".

Limitations
-----------
- C(N, k) explodes quickly: N=10, k=2 → 45 splits; N=10, k=3 → 120.
  Default is N=6, k=2 → 15 splits.  Bumping N improves resolution
  but increases compute.
- Purge + embargo size depends on signal autocorrelation.  Defaults
  are conservative (10 samples each side).
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
CPCV_LOG = LOG_DIR / "l2_cpcv_runs.jsonl"


@dataclass
class CPCVSplit:
    """One train/test split with its score."""

    split_idx: int
    test_fold_indices: tuple[int, ...]
    n_train: int
    n_test: int
    train_score: float
    test_score: float


@dataclass
class CPCVReport:
    n_folds: int
    k_test: int
    n_splits: int
    purge_size: int
    embargo_size: int
    metric_name: str
    test_score_mean: float | None
    test_score_stddev: float | None
    test_score_min: float | None
    test_score_max: float | None
    test_score_median: float | None
    sample_sharpe: float | None  # Deflated by realized n_splits
    splits: list[CPCVSplit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _build_fold_indices(n_samples: int, n_folds: int) -> list[tuple[int, int]]:
    """Split [0, n_samples) into n_folds contiguous ranges.  Returns
    list of (lo, hi) inclusive-exclusive ranges."""
    base = n_samples // n_folds
    rem = n_samples % n_folds
    folds: list[tuple[int, int]] = []
    cursor = 0
    for i in range(n_folds):
        size = base + (1 if i < rem else 0)
        folds.append((cursor, cursor + size))
        cursor += size
    return folds


def _purged_train_indices(
    n_samples: int, test_folds: list[tuple[int, int]], *, purge_size: int = 10, embargo_size: int = 10
) -> list[int]:
    """Return list of training-set sample indices with purge + embargo
    zones around each test fold removed."""
    excluded: set[int] = set()
    for lo, hi in test_folds:
        # Test fold itself
        for i in range(lo, hi):
            excluded.add(i)
        # Purge BEFORE the test fold (prevent training on samples
        # whose labels overlap into test)
        for i in range(max(0, lo - purge_size), lo):
            excluded.add(i)
        # Embargo AFTER the test fold (prevent training on samples
        # whose state was contaminated by test transition)
        for i in range(hi, min(n_samples, hi + embargo_size)):
            excluded.add(i)
    return [i for i in range(n_samples) if i not in excluded]


def _sharpe_from_returns(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    m = statistics.mean(returns)
    var = sum((r - m) ** 2 for r in returns) / max(len(returns) - 1, 1)
    std = var**0.5
    return m / std if std > 0 else 0.0


def cpcv(
    returns: list[float],
    *,
    n_folds: int = 6,
    k_test: int = 2,
    purge_size: int = 10,
    embargo_size: int = 10,
    metric: str = "sharpe",
) -> CPCVReport:
    """Run CPCV over a series of per-trade returns.

    For each combination of k test folds (out of n_folds total),
    compute the chosen metric on (a) the test set and (b) the
    purged training set.  Aggregate test scores into a distribution.
    """
    n = len(returns)
    notes: list[str] = []
    if n < n_folds * 3:
        notes.append(f"Sample {n} too small for {n_folds} folds — need at least 3 samples per fold")
    if n < 20:
        return CPCVReport(
            n_folds=n_folds,
            k_test=k_test,
            n_splits=0,
            purge_size=purge_size,
            embargo_size=embargo_size,
            metric_name=metric,
            test_score_mean=None,
            test_score_stddev=None,
            test_score_min=None,
            test_score_max=None,
            test_score_median=None,
            sample_sharpe=None,
            notes=notes + ["sample too small for CPCV (need n >= 20)"],
        )
    folds = _build_fold_indices(n, n_folds)
    splits: list[CPCVSplit] = []
    test_scores: list[float] = []
    for split_idx, test_fold_indices in enumerate(combinations(range(n_folds), k_test)):
        test_folds = [folds[i] for i in test_fold_indices]
        # Test indices
        test_indices: list[int] = []
        for lo, hi in test_folds:
            test_indices.extend(range(lo, hi))
        test_returns = [returns[i] for i in test_indices]
        # Train indices (purged + embargoed)
        train_indices = _purged_train_indices(n, test_folds, purge_size=purge_size, embargo_size=embargo_size)
        train_returns = [returns[i] for i in train_indices]
        # Score
        if metric == "sharpe":
            train_score = _sharpe_from_returns(train_returns)
            test_score = _sharpe_from_returns(test_returns)
        elif metric == "mean":
            train_score = statistics.mean(train_returns) if train_returns else 0.0
            test_score = statistics.mean(test_returns) if test_returns else 0.0
        else:
            raise ValueError(f"unknown metric: {metric}")
        splits.append(
            CPCVSplit(
                split_idx=split_idx,
                test_fold_indices=tuple(test_fold_indices),
                n_train=len(train_indices),
                n_test=len(test_indices),
                train_score=round(train_score, 4),
                test_score=round(test_score, 4),
            )
        )
        test_scores.append(test_score)

    if not test_scores:
        return CPCVReport(
            n_folds=n_folds,
            k_test=k_test,
            n_splits=0,
            purge_size=purge_size,
            embargo_size=embargo_size,
            metric_name=metric,
            test_score_mean=None,
            test_score_stddev=None,
            test_score_min=None,
            test_score_max=None,
            test_score_median=None,
            sample_sharpe=None,
            notes=notes + ["no splits computed"],
        )

    mean_score = statistics.mean(test_scores)
    stddev_score = statistics.stdev(test_scores) if len(test_scores) >= 2 else 0.0
    median_score = statistics.median(test_scores)
    min_score = min(test_scores)
    max_score = max(test_scores)
    # "Sample sharpe" = mean / stddev across splits.  Tells us how
    # stable the score is across reshuffles.  High mean, low stddev
    # → robust strategy.
    sample_sharpe = mean_score / stddev_score if stddev_score > 0 else 0.0

    return CPCVReport(
        n_folds=n_folds,
        k_test=k_test,
        n_splits=len(splits),
        purge_size=purge_size,
        embargo_size=embargo_size,
        metric_name=metric,
        test_score_mean=round(mean_score, 4),
        test_score_stddev=round(stddev_score, 4),
        test_score_min=round(min_score, 4),
        test_score_max=round(max_score, 4),
        test_score_median=round(median_score, 4),
        sample_sharpe=round(sample_sharpe, 4),
        splits=splits,
        notes=notes,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n-folds", type=int, default=6)
    ap.add_argument("--k-test", type=int, default=2)
    ap.add_argument("--purge", type=int, default=10)
    ap.add_argument("--embargo", type=int, default=10)
    ap.add_argument("--metric", default="sharpe", choices=["sharpe", "mean"])
    ap.add_argument("--input", type=Path, default=None, help="JSON file with array of per-trade returns")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.input and args.input.exists():
        returns = json.loads(args.input.read_text())
    else:
        print("No --input provided; generating synthetic returns for demo")
        import random

        rng = random.Random(42)
        returns = [rng.gauss(0.1, 1.0) for _ in range(100)]

    report = cpcv(
        returns,
        n_folds=args.n_folds,
        k_test=args.k_test,
        purge_size=args.purge,
        embargo_size=args.embargo,
        metric=args.metric,
    )

    try:
        with CPCV_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **asdict(report)}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: cpcv log write failed: {e}", file=sys.stderr)

    if args.json:
        # Trim full splits list for compactness
        out = asdict(report)
        out["splits"] = out["splits"][:5]  # keep first 5
        print(json.dumps(out, indent=2))
        return 0

    print()
    print("=" * 78)
    print(f"CPCV  (n_folds={report.n_folds}, k_test={report.k_test}, metric={report.metric_name})")
    print("=" * 78)
    print(f"  n_splits         : {report.n_splits}")
    print(f"  purge / embargo  : {report.purge_size} / {report.embargo_size}")
    print()
    print("  Test score distribution:")
    print(f"    mean   : {report.test_score_mean}")
    print(f"    stddev : {report.test_score_stddev}")
    print(f"    median : {report.test_score_median}")
    print(f"    min    : {report.test_score_min}")
    print(f"    max    : {report.test_score_max}")
    print()
    print(f"  Sample sharpe (mean/stddev across splits): {report.sample_sharpe}")
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 0


if __name__ == "__main__":
    _ = math  # silence import linter for math
    raise SystemExit(main())
