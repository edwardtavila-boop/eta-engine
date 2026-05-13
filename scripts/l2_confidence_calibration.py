"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_confidence_calibration
================================================================
Brier score + calibration plot for the ``confidence`` field that
each L2 strategy emits on every signal.

Why this exists
---------------
Per the Firm's pre-committed falsification criterion #4:
> Retire if confidence Brier > 0.30 after n_trades >= 100.

The strategies emit ``confidence ∈ [0.0, 1.0]`` based on how far the
signal exceeded its threshold.  Until something scores those numbers
against realized win/loss outcomes, they're decoration.

Brier score = mean squared error between predicted probability of
the EVENT (win) and the realized 0/1 outcome.  Lower is better.
A degenerate "always predict 0.5" baseline gets Brier=0.25.

Calibration buckets
-------------------
We bucket confidence into deciles [0.0-0.1, 0.1-0.2, ..., 0.9-1.0]
and compute the realized win rate per bucket.  A well-calibrated
strategy has each bucket's win rate close to the mid of that bucket
(e.g., 0.5-0.6 confidence → 55% win rate).

A miscalibrated strategy with overconfident predictions has high
buckets with lower-than-expected win rates.

Run
---
::

    python -m eta_engine.scripts.l2_confidence_calibration
    python -m eta_engine.scripts.l2_confidence_calibration --strategy book_imbalance
    python -m eta_engine.scripts.l2_confidence_calibration --json

Reads
-----
- logs/eta_engine/l2_signal_log.jsonl  (confidence per signal)
- logs/eta_engine/broker_fills.jsonl   (realized outcomes via signal_id)

Writes
------
- logs/eta_engine/l2_calibration.jsonl  (one digest per invocation)
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_LOG = LOG_DIR / "l2_signal_log.jsonl"
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
CALIBRATION_LOG = LOG_DIR / "l2_calibration.jsonl"


@dataclass
class CalibrationBucket:
    bucket_label: str  # e.g. "0.5-0.6"
    lo: float
    hi: float
    n: int
    n_wins: int
    realized_win_rate: float | None
    expected_mid: float  # bucket midpoint (the implied prediction)
    deviation: float | None  # realized - expected


@dataclass
class CalibrationReport:
    strategy_id: str | None
    n_observations: int
    brier_score: float | None
    falsification_threshold: float = 0.30
    falsification_triggered: bool = False
    buckets: list[CalibrationBucket] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_jsonl(path: Path, *, since_days: int = 90, strategy_id: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=since_days)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts") or rec.get("timestamp_utc")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                if strategy_id and rec.get("strategy_id") != strategy_id:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


def _build_outcomes(signals: list[dict], fills: list[dict]) -> list[dict]:
    """For each signal, find its terminal fill (TARGET = win, STOP = loss,
    TIMEOUT = computed from final price).  Returns list of dicts:
        {"signal_id", "confidence", "outcome" (1=win, 0=loss)}"""
    # Group fills by signal_id (collect all that share the id)
    fills_by_signal: dict[str, list[dict]] = {}
    for f in fills:
        sid = f.get("signal_id")
        if not sid:
            continue
        fills_by_signal.setdefault(sid, []).append(f)

    outcomes: list[dict] = []
    for sig in signals:
        sid = sig.get("signal_id")
        if not sid or sid not in fills_by_signal:
            continue
        sig_fills = fills_by_signal[sid]
        # Find the TERMINAL fill: TARGET / STOP / TIMEOUT / CANCEL
        terminal = None
        for f in sig_fills:
            er = str(f.get("exit_reason", "")).upper()
            if er in ("TARGET", "STOP", "TIMEOUT", "CANCEL"):
                terminal = f
                break
        if terminal is None:
            continue  # no terminal fill yet (still open)
        exit_reason = str(terminal.get("exit_reason", "")).upper()
        # TARGET = win, STOP = loss, others = neither (skip)
        if exit_reason == "TARGET":
            outcome = 1
        elif exit_reason == "STOP":
            outcome = 0
        else:
            continue
        outcomes.append(
            {
                "signal_id": sid,
                "confidence": float(sig.get("confidence", 0.0)),
                "outcome": outcome,
            }
        )
    return outcomes


def compute_brier_score(outcomes: list[dict]) -> float | None:
    """Brier score = mean( (predicted - actual)^2 ).
    Returns None when sample too small (n < 10)."""
    if len(outcomes) < 10:
        return None
    sse = sum((o["confidence"] - o["outcome"]) ** 2 for o in outcomes)
    return round(sse / len(outcomes), 4)


def build_buckets(outcomes: list[dict]) -> list[CalibrationBucket]:
    """Decile-bucket the outcomes and compute realized win rate per bucket."""
    n_buckets = 10
    width = 1.0 / n_buckets
    buckets: list[CalibrationBucket] = []
    for i in range(n_buckets):
        lo = i * width
        hi = (i + 1) * width
        # Last bucket includes 1.0
        in_bucket = [
            o for o in outcomes if (lo <= o["confidence"] < hi or (i == n_buckets - 1 and o["confidence"] == 1.0))
        ]
        n = len(in_bucket)
        n_wins = sum(o["outcome"] for o in in_bucket)
        rwr = n_wins / n if n > 0 else None
        mid = (lo + hi) / 2
        dev = (rwr - mid) if rwr is not None else None
        buckets.append(
            CalibrationBucket(
                bucket_label=f"{lo:.1f}-{hi:.1f}",
                lo=round(lo, 2),
                hi=round(hi, 2),
                n=n,
                n_wins=n_wins,
                realized_win_rate=round(rwr, 3) if rwr is not None else None,
                expected_mid=round(mid, 2),
                deviation=round(dev, 3) if dev is not None else None,
            )
        )
    return buckets


def run_calibration(
    strategy_id: str | None = None, *, since_days: int = 90, falsification_threshold: float = 0.30
) -> CalibrationReport:
    signals = _read_jsonl(SIGNAL_LOG, since_days=since_days, strategy_id=strategy_id)
    fills = _read_jsonl(BROKER_FILL_LOG, since_days=since_days)
    outcomes = _build_outcomes(signals, fills)
    brier = compute_brier_score(outcomes)
    buckets = build_buckets(outcomes) if outcomes else []
    warnings: list[str] = []
    if len(outcomes) < 100:
        warnings.append(
            f"Only {len(outcomes)} matched outcomes — Brier-based falsification criterion requires n >= 100."
        )
    triggered = brier is not None and brier > falsification_threshold and len(outcomes) >= 100
    return CalibrationReport(
        strategy_id=strategy_id,
        n_observations=len(outcomes),
        brier_score=brier,
        falsification_threshold=falsification_threshold,
        falsification_triggered=triggered,
        buckets=buckets,
        warnings=warnings,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default=None, help="Filter to one strategy_id (default: all)")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--threshold", type=float, default=0.30, help="Falsification Brier threshold (default 0.30)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = run_calibration(
        strategy_id=args.strategy,
        since_days=args.days,
        falsification_threshold=args.threshold,
    )
    # Persist
    try:
        with CALIBRATION_LOG.open("a", encoding="utf-8") as f:
            digest = {
                "ts": datetime.now(UTC).isoformat(),
                "strategy_id": report.strategy_id,
                "n_observations": report.n_observations,
                "brier_score": report.brier_score,
                "falsification_triggered": report.falsification_triggered,
            }
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: could not append calibration digest: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 1 if report.falsification_triggered else 0

    print()
    print("=" * 78)
    print(f"L2 CONFIDENCE CALIBRATION  ({datetime.now(UTC).isoformat()})")
    print("=" * 78)
    print(f"  strategy_id     : {report.strategy_id or '<all>'}")
    print(f"  n_observations  : {report.n_observations}")
    print(f"  brier_score     : {report.brier_score}")
    print(
        f"  falsification   : {'TRIGGERED' if report.falsification_triggered else 'ok'}"
        f"  (threshold > {report.falsification_threshold})"
    )
    print()
    if report.buckets:
        print(f"  {'Bucket':<10s} {'n':<6s} {'wins':<6s} {'realized':<10s} {'expected':<10s} {'deviation':<10s}")
        print(f"  {'-' * 10:<10s} {'-' * 6:<6s} {'-' * 6:<6s} {'-' * 10:<10s} {'-' * 10:<10s} {'-' * 10:<10s}")
        for b in report.buckets:
            rwr = f"{b.realized_win_rate:.3f}" if b.realized_win_rate is not None else "n/a"
            dev = f"{b.deviation:+.3f}" if b.deviation is not None else "n/a"
            print(f"  {b.bucket_label:<10s} {b.n:<6d} {b.n_wins:<6d} {rwr:<10s} {b.expected_mid:<10.2f} {dev:<10s}")
    if report.warnings:
        print()
        print("  WARNINGS:")
        for w in report.warnings:
            print(f"    - {w}")
    print()
    return 1 if report.falsification_triggered else 0


if __name__ == "__main__":
    raise SystemExit(main())
