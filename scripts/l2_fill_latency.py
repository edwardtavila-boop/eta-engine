"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_fill_latency
======================================================
Measures signal→fill latency for L2 strategies and flags when it
exceeds the strategy's edge-decay window.

Why this exists
---------------
The microprice_drift strategy is a fast scalp: its edge decays over
~5-15 seconds.  If the signal→fill latency exceeds the decay window,
the strategy is *literally trading on stale information* — entering
when the microprice dislocation has already reverted.

The book_imbalance strategy is less latency-sensitive (15s of
conviction before fire), but still bleeds edge if fills lag >10s.

This script reads signals + fills, computes per-strategy latency
distributions, and flags strategies whose p90 latency exceeds the
strategy-specific decay threshold.

Per-strategy decay thresholds
-----------------------------
- microprice_drift   : 3.0s  (fast scalp; edge is ~5s)
- book_imbalance     : 5.0s  (slower; edge is ~15s)
- footprint_absorption: 5.0s (similar to book_imbalance)
- aggressor_flow     : 10.0s (bar-paced; edge survives longer)

Verdict
-------
- OK         : p90 latency <= 0.5 * threshold (very fast)
- ACCEPTABLE : p90 latency <= threshold
- MARGINAL   : p90 latency <= 2.0 * threshold (edge bleeding)
- FAIL       : p90 latency >  2.0 * threshold (strategy executing on stale signal)

Output
------
- Per-strategy latency report (text + JSON)
- Append to logs/eta_engine/l2_fill_latency.jsonl

Run
---
::

    python -m eta_engine.scripts.l2_fill_latency
    python -m eta_engine.scripts.l2_fill_latency --strategy microprice_drift_v1
    python -m eta_engine.scripts.l2_fill_latency --json
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

SIGNAL_LOG = LOG_DIR / "l2_signal_log.jsonl"
BROKER_FILL_LOG = LOG_DIR / "broker_fills.jsonl"
LATENCY_LOG = LOG_DIR / "l2_fill_latency.jsonl"


# Per-strategy edge-decay thresholds in seconds
DECAY_THRESHOLDS = {
    "microprice_drift_v1": 3.0,
    "book_imbalance_v1": 5.0,
    "footprint_absorption_v1": 5.0,
    "aggressor_flow_v1": 10.0,
}
DEFAULT_DECAY_THRESHOLD = 5.0


@dataclass
class LatencyReport:
    strategy_id: str | None
    n_observations: int
    p50_latency_s: float | None
    p90_latency_s: float | None
    p99_latency_s: float | None
    max_latency_s: float | None
    decay_threshold_s: float
    verdict: str
    notes: list[str] = field(default_factory=list)


def _read_jsonl(path: Path, *, since_days: int = 30, strategy_id: str | None = None) -> list[dict]:
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
                rec["_parsed_ts"] = dt
                out.append(rec)
    except OSError:
        return []
    return out


def _compute_latencies(signals: list[dict], fills: list[dict]) -> list[float]:
    """Match signals to FIRST entry fill by signal_id, return latency
    in seconds.  Only considers ENTRY fills (not exit fills)."""
    sig_by_id: dict[str, datetime] = {}
    for s in signals:
        sid = s.get("signal_id")
        if not sid:
            continue
        sig_by_id[sid] = s["_parsed_ts"]
    latencies: list[float] = []
    seen_sigs: set[str] = set()
    for f in fills:
        sid = f.get("signal_id")
        if not sid or sid in seen_sigs:
            continue
        if str(f.get("exit_reason", "")).upper() != "ENTRY":
            continue
        if sid not in sig_by_id:
            continue
        seen_sigs.add(sid)
        latency = (f["_parsed_ts"] - sig_by_id[sid]).total_seconds()
        if latency >= 0:  # defensive — negative latency = clock skew
            latencies.append(latency)
    return latencies


def _percentile(sorted_data: list[float], pct: float) -> float | None:
    if not sorted_data:
        return None
    idx = int(pct / 100 * len(sorted_data))
    idx = max(0, min(len(sorted_data) - 1, idx))
    return sorted_data[idx]


def _verdict(p90: float | None, threshold: float) -> str:
    if p90 is None:
        return "INSUFFICIENT"
    if p90 <= 0.5 * threshold:
        return "OK"
    if p90 <= threshold:
        return "ACCEPTABLE"
    if p90 <= 2.0 * threshold:
        return "MARGINAL"
    return "FAIL"


def run_latency_audit(
    strategy_id: str | None = None,
    *,
    since_days: int = 30,
    _signal_path: Path | None = None,
    _fill_path: Path | None = None,
    _override_threshold: float | None = None,
) -> LatencyReport:
    signals = _read_jsonl(
        _signal_path if _signal_path is not None else SIGNAL_LOG, since_days=since_days, strategy_id=strategy_id
    )
    fills = _read_jsonl(_fill_path if _fill_path is not None else BROKER_FILL_LOG, since_days=since_days)
    latencies = _compute_latencies(signals, fills)
    threshold = _override_threshold or DECAY_THRESHOLDS.get(strategy_id, DEFAULT_DECAY_THRESHOLD)
    if not latencies:
        return LatencyReport(
            strategy_id=strategy_id,
            n_observations=0,
            p50_latency_s=None,
            p90_latency_s=None,
            p99_latency_s=None,
            max_latency_s=None,
            decay_threshold_s=threshold,
            verdict="INSUFFICIENT",
            notes=["no matched signal/fill pairs found"],
        )
    sorted_lat = sorted(latencies)
    p50 = statistics.median(latencies)
    p90 = _percentile(sorted_lat, 90)
    p99 = _percentile(sorted_lat, 99)
    mx = max(latencies)
    verdict = _verdict(p90, threshold)
    notes: list[str] = []
    if len(latencies) < 30:
        notes.append(f"Only {len(latencies)} observations — verdict is statistically weak below n=30")
    if verdict in ("MARGINAL", "FAIL"):
        notes.append(
            f"p90 latency {p90:.2f}s exceeds decay threshold {threshold:.1f}s; "
            "strategy may be trading on stale signal information."
        )
    return LatencyReport(
        strategy_id=strategy_id,
        n_observations=len(latencies),
        p50_latency_s=round(p50, 3),
        p90_latency_s=round(p90, 3) if p90 is not None else None,
        p99_latency_s=round(p99, 3) if p99 is not None else None,
        max_latency_s=round(mx, 3),
        decay_threshold_s=threshold,
        verdict=verdict,
        notes=notes,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default=None, help="Filter to one strategy_id (default: all)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--threshold", type=float, default=None, help="Override decay threshold in seconds")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = run_latency_audit(
        strategy_id=args.strategy,
        since_days=args.days,
        _override_threshold=args.threshold,
    )

    try:
        with LATENCY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **asdict(report)}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: latency log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0 if report.verdict in ("OK", "ACCEPTABLE") else 1

    print()
    print("=" * 78)
    print(f"L2 FILL LATENCY AUDIT  (strategy={report.strategy_id or 'all'})")
    print("=" * 78)
    print(f"  n_observations    : {report.n_observations}")
    print(f"  p50 latency       : {report.p50_latency_s}s")
    print(f"  p90 latency       : {report.p90_latency_s}s")
    print(f"  p99 latency       : {report.p99_latency_s}s")
    print(f"  max latency       : {report.max_latency_s}s")
    print(f"  decay threshold   : {report.decay_threshold_s}s")
    print(f"  verdict           : {report.verdict}")
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 0 if report.verdict in ("OK", "ACCEPTABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
