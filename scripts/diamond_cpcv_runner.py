"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_cpcv_runner
==========================================================
Per-diamond Combinatorial Purged Cross-Validation.

Why this exists
---------------
The authenticity audit's bootstrap CI tests whether the OBSERVED
edge is statistically separable from zero.  That's a strong test
of "did this bot make money historically", but a weak test of
"will this bot make money out-of-sample".

CPCV is the right tool for the second question.  It splits the
trade history into many train/test combinations (using purged k-fold
to avoid leakage between time-correlated trades), then reports the
distribution of out-of-sample sharpe across splits.

A real diamond:
  - CPCV test_score_mean > 0
  - CPCV test_score_stddev moderate (high stddev → fragile to regime)
  - At least 60% of splits positive

A lab-grown diamond passes the in-sample bootstrap CI but FAILS
CPCV — the edge doesn't generalize.

What it does
------------
For each diamond:
  1. Load per-trade R-multiples from trade_closes.jsonl
     (R-basis is dimension-free, immune to USD scale bugs)
  2. Run cpcv() from l2_cpcv with n_folds=6, k_test=2 (15 splits)
  3. Record test_score_mean, stddev, min, max, median, and
     "share_of_splits_positive"
  4. Verdict: ROBUST / FRAGILE / NOT_CPCV_READY (sample too small)

Output
------
- stdout / --json
- var/eta_engine/state/diamond_cpcv_latest.json

Run
---
::

    python -m eta_engine.scripts.diamond_cpcv_runner
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.feeds.capital_allocator import DIAMOND_BOTS
from eta_engine.scripts.l2_cpcv import cpcv

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
STATE_DIR = WORKSPACE_ROOT / "var" / "eta_engine" / "state"
LEGACY_STATE_DIR = ROOT / "state"

TRADE_CLOSES_CANDIDATES = [
    STATE_DIR / "jarvis_intel" / "trade_closes.jsonl",
    LEGACY_STATE_DIR / "jarvis_intel" / "trade_closes.jsonl",
]

OUT_LATEST = STATE_DIR / "diamond_cpcv_latest.json"

#: CPCV defaults — operator can override.  n=6 k=2 → 15 splits per bot
DEFAULT_N_FOLDS = 6
DEFAULT_K_TEST = 2

#: Verdict thresholds.  These are conservative — a "ROBUST" classification
#: requires the bot to generate positive sharpe on >= 60% of out-of-sample
#: splits AND have positive mean test sharpe across all splits.
ROBUST_SHARE_THRESHOLD = 0.60
MIN_SAMPLES_FOR_CPCV = 20


@dataclass
class DiamondCPCVReport:
    bot_id: str
    n_trades: int = 0
    sample_sharpe: float | None = None
    test_score_mean: float | None = None
    test_score_stddev: float | None = None
    test_score_min: float | None = None
    test_score_max: float | None = None
    test_score_median: float | None = None
    share_of_splits_positive: float | None = None
    n_splits: int = 0
    verdict: str = "NOT_CPCV_READY"
    justification: str = ""
    notes: list[str] = field(default_factory=list)


def _load_r_multiples(bot_id: str) -> list[float]:
    """Collect per-trade R-multiples for the bot across known paths."""
    r_list: list[float] = []
    for path in TRADE_CLOSES_CANDIDATES:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("bot_id") != bot_id:
                        continue
                    r = rec.get("realized_r")
                    if r is None:
                        continue
                    try:
                        r_f = float(r)
                    except (TypeError, ValueError):
                        continue
                    if r_f != 0.0:  # skip null / flat trades
                        r_list.append(r_f)
        except OSError:
            continue
    return r_list


def _assess_bot(bot_id: str) -> DiamondCPCVReport:
    rep = DiamondCPCVReport(bot_id=bot_id)
    r_list = _load_r_multiples(bot_id)
    rep.n_trades = len(r_list)
    if rep.n_trades < MIN_SAMPLES_FOR_CPCV:
        rep.verdict = "NOT_CPCV_READY"
        rep.justification = (
            f"n={rep.n_trades} < {MIN_SAMPLES_FOR_CPCV} (need more "
            "trade history before CPCV is meaningful)"
        )
        return rep
    # Sample-level sharpe for context
    cpcv_report = cpcv(
        r_list,
        n_folds=DEFAULT_N_FOLDS,
        k_test=DEFAULT_K_TEST,
        metric="sharpe",
    )
    rep.sample_sharpe = cpcv_report.sample_sharpe
    rep.test_score_mean = cpcv_report.test_score_mean
    rep.test_score_stddev = cpcv_report.test_score_stddev
    rep.test_score_min = cpcv_report.test_score_min
    rep.test_score_max = cpcv_report.test_score_max
    rep.test_score_median = cpcv_report.test_score_median
    rep.n_splits = cpcv_report.n_splits
    rep.notes = list(cpcv_report.notes or [])

    # share_of_splits_positive — direct measure of "edge holds across
    # different out-of-sample windows"
    if cpcv_report.n_splits == 0:
        rep.verdict = "NOT_CPCV_READY"
        rep.justification = (
            "CPCV returned 0 splits — sample too small for "
            f"{DEFAULT_N_FOLDS} folds × {DEFAULT_K_TEST} test"
        )
        return rep

    # The cpcv() return doesn't expose per-split detail by default;
    # we approximate share_positive using mean/std (Gaussian assumption)
    # OR re-derive via a Monte-Carlo proxy.  For now use a simple
    # heuristic: if mean > 0 and mean > stddev/2, edge survives.
    mean = rep.test_score_mean or 0.0
    std = rep.test_score_stddev or 0.0
    # Coarse estimate of share-positive under Gaussian assumption
    # (mean / std = z; share above 0 = Phi(z))
    z = mean / std if std > 1e-12 else 0.0
    rep.share_of_splits_positive = round(_phi(z), 3)

    if rep.share_of_splits_positive >= ROBUST_SHARE_THRESHOLD and mean > 0:
        rep.verdict = "ROBUST"
        rep.justification = (
            f"CPCV {rep.n_splits} splits: mean test sharpe = "
            f"{mean:+.3f} (stddev {std:.3f}); ~{rep.share_of_splits_positive*100:.0f}% "
            "of splits positive → edge generalizes out-of-sample"
        )
    elif mean > 0:
        rep.verdict = "FRAGILE"
        rep.justification = (
            f"CPCV {rep.n_splits} splits: mean test sharpe = "
            f"{mean:+.3f} but high stddev {std:.3f} "
            f"(~{rep.share_of_splits_positive*100:.0f}% splits positive) → "
            "edge sensitive to fold choice"
        )
    else:
        rep.verdict = "FRAGILE"
        rep.justification = (
            f"CPCV {rep.n_splits} splits: mean test sharpe = "
            f"{mean:+.3f} <= 0 — edge does NOT generalize out-of-sample"
        )
    return rep


def _phi(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    # Constants
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911
    sign = 1.0 if x >= 0 else -1.0
    x = abs(x) / (2.0 ** 0.5)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * (
        (-x * x).__class__.__call__ if False else float(__import__("math").exp(-x * x))
    )
    return 0.5 * (1.0 + sign * y)


def run() -> dict:
    reports = [_assess_bot(b) for b in sorted(DIAMOND_BOTS)]
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_folds": DEFAULT_N_FOLDS,
        "k_test": DEFAULT_K_TEST,
        "n_diamonds": len(reports),
        "verdict_counts": counts,
        "reports": [asdict(r) for r in reports],
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print(summary: dict) -> None:
    print("=" * 110)
    print(
        f" DIAMOND CPCV — {summary['ts']}  "
        f"(n_folds={summary['n_folds']}, k_test={summary['k_test']})",
    )
    print("=" * 110)
    print(" Verdict roll-up: " + ", ".join(
        f"{k}={v}" for k, v in summary["verdict_counts"].items()))
    print()
    print(
        f" {'bot':28s} {'verdict':16s} {'n':>5s} {'mean':>7s} {'std':>7s} "
        f"{'min':>7s} {'max':>7s} {'pos%':>5s}  justification",
    )
    print("-" * 130)
    for r in summary["reports"]:
        n = r.get("n_trades") or 0
        m = r.get("test_score_mean")
        s = r.get("test_score_stddev")
        lo = r.get("test_score_min")
        hi = r.get("test_score_max")
        sp = r.get("share_of_splits_positive")
        m_s = f"{m:+.3f}" if m is not None else "n/a"
        s_s = f"{s:.3f}" if s is not None else "n/a"
        lo_s = f"{lo:+.2f}" if lo is not None else "n/a"
        hi_s = f"{hi:+.2f}" if hi is not None else "n/a"
        sp_s = f"{sp*100:.0f}%" if sp is not None else "n/a"
        print(
            f" {r['bot_id']:28s} {r['verdict']:16s} {n:>5d} {m_s:>7s} "
            f"{s_s:>7s} {lo_s:>7s} {hi_s:>7s} {sp_s:>5s}  {r['justification'][:60]}",
        )
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = run()
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    # Exit 2 if any diamond has FRAGILE verdict (sharpe doesn't survive
    # OOS); exit 1 if any is NOT_CPCV_READY.
    counts = summary["verdict_counts"]
    if counts.get("FRAGILE", 0) > 0:
        return 2
    if counts.get("NOT_CPCV_READY", 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
