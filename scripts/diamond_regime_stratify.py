"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_regime_stratify
==============================================================
Per-regime / per-session breakdown of every diamond bot's edge.

Why this exists
---------------
The CPCV runner says "is the edge real out-of-sample".  It does NOT
say "in which regime does the edge actually live".  A bot can have
mean OOS sharpe +0.20 (looks GENUINE / ROBUST) and still be:

  - All-edge-in-trending-up regime, zero edge in chop → don't run
    it during ranges
  - All-edge-in-RTH, negative in ETH → restrict by session
  - All-edge-in-Wednesday-EIA, zero on other days → restrict by
    day-of-week

The trade_closes.jsonl carries `regime` and `session` fields per
close.  This runner buckets per-trade R-multiples by those fields
and reports cumulative + per-bucket mean R + Bootstrap CI per
bucket.  Output guides session/regime gating decisions per diamond.

What it reports
---------------
For each diamond, per bucket (regime × session):
  - n_trades
  - cumulative_r
  - mean_r per trade
  - win_rate_pct
  - bootstrap 95% CI on mean R (when n >= 10)

Verdict colors:
  - STRONG    — n>=20 AND CI lower > +0.10R (clear positive edge)
  - WEAK      — n>=10 AND CI lower > 0      (positive but marginal)
  - NULL      — n>=10 AND CI lower <= 0     (no separable edge)
  - SPARSE    — n<10                        (insufficient sample)

Output
------
- stdout / --json
- var/eta_engine/state/diamond_regime_stratify_latest.json

Run
---
::

    python -m eta_engine.scripts.diamond_regime_stratify
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.feeds.capital_allocator import DIAMOND_BOTS

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
STATE_DIR = WORKSPACE_ROOT / "var" / "eta_engine" / "state"
LEGACY_STATE_DIR = ROOT / "state"

TRADE_CLOSES_CANDIDATES = [
    STATE_DIR / "jarvis_intel" / "trade_closes.jsonl",
    LEGACY_STATE_DIR / "jarvis_intel" / "trade_closes.jsonl",
]
OUT_LATEST = STATE_DIR / "diamond_regime_stratify_latest.json"

MIN_N_FOR_BOOTSTRAP = 10
STRONG_CI_LOWER_THRESHOLD = 0.10
BOOTSTRAP_N = 1000
RANDOM_SEED = 42


@dataclass
class BucketStats:
    bucket_key: str
    regime: str
    session: str
    n_trades: int
    cumulative_r: float
    mean_r: float
    win_rate_pct: float
    bootstrap_ci_lower: float | None = None
    bootstrap_ci_upper: float | None = None
    verdict: str = "SPARSE"


@dataclass
class BotStratifyReport:
    bot_id: str
    total_trades: int
    total_r: float
    buckets: list[BucketStats] = field(default_factory=list)
    strongest_bucket: str | None = None
    weakest_bucket: str | None = None
    notes: list[str] = field(default_factory=list)


def _bootstrap_ci_mean(
    samples: list[float], n_resamples: int = BOOTSTRAP_N, confidence: float = 0.95
) -> tuple[float, float]:
    rng = random.Random(RANDOM_SEED)
    n = len(samples)
    means: list[float] = []
    for _ in range(n_resamples):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    alpha = (1 - confidence) / 2
    lo_idx = int(alpha * n_resamples)
    hi_idx = int((1 - alpha) * n_resamples) - 1
    return means[lo_idx], means[hi_idx]


def _classify(bucket: BucketStats) -> None:
    """STRONG / WEAK / NULL / SPARSE based on n + bootstrap CI."""
    if bucket.n_trades < MIN_N_FOR_BOOTSTRAP:
        bucket.verdict = "SPARSE"
        return
    if bucket.bootstrap_ci_lower is None:
        bucket.verdict = "SPARSE"
        return
    if bucket.bootstrap_ci_lower > STRONG_CI_LOWER_THRESHOLD and bucket.n_trades >= 20:
        bucket.verdict = "STRONG"
    elif bucket.bootstrap_ci_lower > 0:
        bucket.verdict = "WEAK"
    else:
        bucket.verdict = "NULL"


def _load_records(bot_id: str) -> list[dict]:
    out: list[dict] = []
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
                    out.append(rec)
        except OSError:
            continue
    return out


def _stratify_bot(bot_id: str) -> BotStratifyReport:
    rep = BotStratifyReport(bot_id=bot_id, total_trades=0, total_r=0.0)
    records = _load_records(bot_id)
    rep.total_trades = len(records)
    if not records:
        rep.notes.append("no trade-close records found")
        return rep

    # Bucket by (regime, session)
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for rec in records:
        regime = str(rec.get("regime") or "unknown")
        session = str(rec.get("session") or "unknown")
        try:
            r = float(rec.get("realized_r") or 0.0)
        except (TypeError, ValueError):
            continue
        buckets[(regime, session)].append(r)
        rep.total_r += r

    # Build per-bucket stats
    for (regime, session), rs in sorted(buckets.items()):
        n = len(rs)
        cum = sum(rs)
        mean_r = cum / max(n, 1)
        wins = sum(1 for r in rs if r > 0)
        wr = round(100.0 * wins / max(n, 1), 2)
        bucket = BucketStats(
            bucket_key=f"{regime}|{session}",
            regime=regime,
            session=session,
            n_trades=n,
            cumulative_r=round(cum, 4),
            mean_r=round(mean_r, 4),
            win_rate_pct=wr,
        )
        if n >= MIN_N_FOR_BOOTSTRAP:
            lo, hi = _bootstrap_ci_mean(rs)
            bucket.bootstrap_ci_lower = round(lo, 4)
            bucket.bootstrap_ci_upper = round(hi, 4)
        _classify(bucket)
        rep.buckets.append(bucket)

    # Identify strongest + weakest buckets (by cumulative_r among
    # those with n>=10; otherwise by mean_r)
    eligible = [b for b in rep.buckets if b.n_trades >= MIN_N_FOR_BOOTSTRAP]
    if eligible:
        strongest = max(eligible, key=lambda b: b.cumulative_r)
        weakest = min(eligible, key=lambda b: b.cumulative_r)
        rep.strongest_bucket = strongest.bucket_key
        rep.weakest_bucket = weakest.bucket_key
    return rep


def run() -> dict:
    reports = [_stratify_bot(b) for b in sorted(DIAMOND_BOTS)]
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "n_diamonds": len(reports),
        "reports": [asdict(r) for r in reports],
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print(summary: dict) -> None:
    print("=" * 100)
    print(f" DIAMOND REGIME STRATIFICATION — {summary['ts']}")
    print("=" * 100)
    for r in summary["reports"]:
        print(f"\n  {r['bot_id']} — {r['total_trades']} trades, cumR={r['total_r']:+.2f}")
        if not r["buckets"]:
            print("    (no buckets)")
            continue
        print(
            f"    {'regime':14s} {'session':12s} "
            f"{'n':>5s} {'cumR':>8s} {'meanR':>7s} "
            f"{'WR%':>5s} {'CI lo':>7s} verdict",
        )
        # Sort buckets by cumulative R descending
        for b in sorted(r["buckets"], key=lambda x: -x["cumulative_r"]):
            ci_s = f"{b['bootstrap_ci_lower']:+.3f}" if b.get("bootstrap_ci_lower") is not None else "n/a"
            print(
                f"    {b['regime']:14s} {b['session']:12s} "
                f"{b['n_trades']:>5d} {b['cumulative_r']:>+8.2f} "
                f"{b['mean_r']:>+7.3f} {b['win_rate_pct']:>5.1f} "
                f"{ci_s:>7s} {b['verdict']}",
            )
        if r.get("strongest_bucket"):
            print(
                f"    -> STRONGEST: {r['strongest_bucket']}  WEAKEST: {r['weakest_bucket']}",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
