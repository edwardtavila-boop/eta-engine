"""
EVOLUTIONARY TRADING ALGO  //  scripts.l2_cross_broker_arb
==========================================================
Cross-broker price discrepancy detector for the same product
trading on different venues.

Why this exists
---------------
The same product trades on multiple venues:
  - MNQ on IBKR (CME)
  - MNQ on Tradovate (CME; DORMANT unless explicitly reactivated)
  - BTC futures on CME vs ICE vs offshore venues
  - Index spreads (MNQ vs NQ vs MES)

When two venues show different prices for the same product, it's
either:
  - Real arb opportunity (rare, fast)
  - Data lag on one venue (slower exchange feed)
  - Subscription mismatch (delayed vs realtime)

This module compares depth snapshots from two sources, flags when
the mid-price discrepancy exceeds a threshold, and emits an alert.

It does NOT execute arb trades — that requires:
  - Multi-broker order routing (existing eta_engine venue layer)
  - Confirmed real-time data on BOTH sides
  - Latency-sensitive execution

Instead, this is a MONITOR.  Persistent discrepancies indicate a
data quality issue on one venue; transient ones might be real arb.

Inputs
------
- depth file 1: mnq_data/depth/<sym>_<date>.jsonl (primary)
- depth file 2: (optional) different broker's depth feed at a
  different path or rebroadcast via secondary capture daemon

When only one feed exists, the script computes intra-product
spreads (e.g. MNQ vs NQ) instead of cross-broker.

Run
---
::

    python -m eta_engine.scripts.l2_cross_broker_arb \\
        --symbol-a MNQ --symbol-b NQ --date 20260511 \\
        --threshold-bps 5
"""

from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"
ARB_LOG = LOG_DIR / "l2_cross_broker_arb.jsonl"


@dataclass
class DiscrepancyEvent:
    epoch_s: float
    mid_a: float
    mid_b: float
    spread_a: float
    spread_b: float
    diff_pts: float
    diff_bps: float


@dataclass
class CrossBrokerReport:
    symbol_a: str
    symbol_b: str
    date: str
    n_pairs_checked: int
    n_discrepancies: int
    discrepancy_pct: float
    threshold_bps: float
    mean_diff_bps: float | None
    p90_diff_bps: float | None
    max_diff_bps: float | None
    events: list[DiscrepancyEvent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _read_depth_snaps(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _pair_by_time(
    snaps_a: list[dict], snaps_b: list[dict], *, max_skew_seconds: float = 2.0
) -> list[tuple[dict, dict]]:
    """Time-pair two snapshot streams.  For each snap in A, find the
    closest snap in B within ``max_skew_seconds``.  Returns aligned pairs.

    Snaps are assumed sorted by epoch_s.  Uses two-pointer walk for
    O(N+M) instead of O(N*M).
    """
    pairs: list[tuple[dict, dict]] = []
    snaps_a_sorted = sorted(snaps_a, key=lambda s: s.get("epoch_s", 0))
    snaps_b_sorted = sorted(snaps_b, key=lambda s: s.get("epoch_s", 0))
    j = 0
    for a in snaps_a_sorted:
        ea = a.get("epoch_s", 0)
        # Advance j until we find the snap in B with closest epoch
        while j + 1 < len(snaps_b_sorted) and abs(snaps_b_sorted[j + 1].get("epoch_s", 0) - ea) < abs(
            snaps_b_sorted[j].get("epoch_s", 0) - ea
        ):
            j += 1
        if j < len(snaps_b_sorted):
            b = snaps_b_sorted[j]
            if abs(b.get("epoch_s", 0) - ea) <= max_skew_seconds:
                pairs.append((a, b))
    return pairs


def check_discrepancy(
    snaps_a: list[dict],
    snaps_b: list[dict],
    *,
    threshold_bps: float = 5.0,
    max_skew_seconds: float = 2.0,
    max_events: int = 100,
) -> CrossBrokerReport:
    """Compare two depth streams; flag mid-price discrepancies exceeding
    threshold_bps."""
    pairs = _pair_by_time(snaps_a, snaps_b, max_skew_seconds=max_skew_seconds)
    if not pairs:
        return CrossBrokerReport(
            symbol_a="?",
            symbol_b="?",
            date="?",
            n_pairs_checked=0,
            n_discrepancies=0,
            discrepancy_pct=0.0,
            threshold_bps=threshold_bps,
            mean_diff_bps=None,
            p90_diff_bps=None,
            max_diff_bps=None,
            notes=["no time-aligned snapshot pairs"],
        )

    events: list[DiscrepancyEvent] = []
    all_diff_bps: list[float] = []
    for a, b in pairs:
        mid_a = float(a.get("mid", 0))
        mid_b = float(b.get("mid", 0))
        if mid_a <= 0 or mid_b <= 0:
            continue
        diff_pts = mid_a - mid_b
        # Use average mid as the basis for bps
        avg_mid = (mid_a + mid_b) / 2
        diff_bps = abs(diff_pts) / avg_mid * 10000
        all_diff_bps.append(diff_bps)
        if diff_bps >= threshold_bps and len(events) < max_events:
            events.append(
                DiscrepancyEvent(
                    epoch_s=a.get("epoch_s", 0),
                    mid_a=mid_a,
                    mid_b=mid_b,
                    spread_a=float(a.get("spread", 0)),
                    spread_b=float(b.get("spread", 0)),
                    diff_pts=round(diff_pts, 4),
                    diff_bps=round(diff_bps, 2),
                )
            )

    sorted_bps = sorted(all_diff_bps)
    n_discrepancies = sum(1 for d in all_diff_bps if d >= threshold_bps)
    p90_idx = int(0.9 * len(sorted_bps))
    p90_idx = max(0, min(len(sorted_bps) - 1, p90_idx))

    return CrossBrokerReport(
        symbol_a="?",
        symbol_b="?",
        date="?",
        n_pairs_checked=len(pairs),
        n_discrepancies=n_discrepancies,
        discrepancy_pct=round(n_discrepancies / len(pairs) * 100, 2),
        threshold_bps=threshold_bps,
        mean_diff_bps=round(statistics.mean(all_diff_bps), 3) if all_diff_bps else None,
        p90_diff_bps=round(sorted_bps[p90_idx], 3) if sorted_bps else None,
        max_diff_bps=round(max(all_diff_bps), 3) if all_diff_bps else None,
        events=events,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol-a", default="MNQ")
    ap.add_argument("--symbol-b", default="NQ", help="Second symbol (or different broker for same symbol)")
    ap.add_argument("--date", default=None, help="YYYYMMDD (default: today)")
    ap.add_argument("--threshold-bps", type=float, default=5.0)
    ap.add_argument("--max-skew-seconds", type=float, default=2.0)
    ap.add_argument("--path-a", type=Path, default=None, help="Override depth file path for symbol_a")
    ap.add_argument("--path-b", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    date_str = args.date or datetime.now(UTC).strftime("%Y%m%d")
    path_a = args.path_a or (DEPTH_DIR / f"{args.symbol_a}_{date_str}.jsonl")
    path_b = args.path_b or (DEPTH_DIR / f"{args.symbol_b}_{date_str}.jsonl")
    snaps_a = _read_depth_snaps(path_a)
    snaps_b = _read_depth_snaps(path_b)
    report = check_discrepancy(
        snaps_a,
        snaps_b,
        threshold_bps=args.threshold_bps,
        max_skew_seconds=args.max_skew_seconds,
    )
    report.symbol_a = args.symbol_a
    report.symbol_b = args.symbol_b
    report.date = date_str

    try:
        with ARB_LOG.open("a", encoding="utf-8") as f:
            digest = asdict(report)
            digest["events"] = digest["events"][:10]  # trim
            f.write(json.dumps({"ts": datetime.now(UTC).isoformat(), **digest}, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"WARN: arb log write failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return 0

    print()
    print("=" * 78)
    print(f"L2 CROSS-BROKER ARB MONITOR  ({report.symbol_a} vs {report.symbol_b})")
    print("=" * 78)
    print(f"  date              : {report.date}")
    print(f"  pairs checked     : {report.n_pairs_checked:,}")
    print(f"  discrepancies     : {report.n_discrepancies:,} ({report.discrepancy_pct}%)")
    print(f"  threshold         : {report.threshold_bps} bps")
    print(f"  mean diff (bps)   : {report.mean_diff_bps}")
    print(f"  p90 diff (bps)    : {report.p90_diff_bps}")
    print(f"  max diff (bps)    : {report.max_diff_bps}")
    if report.events:
        print()
        print("  Top discrepancies:")
        for e in report.events[:5]:
            print(
                f"    t={e.epoch_s:.0f}  diff={e.diff_pts:+.4f}pts"
                f" ({e.diff_bps:+.2f}bps)  mid_a={e.mid_a} mid_b={e.mid_b}"
            )
    if report.notes:
        print()
        print("  Notes:")
        for n in report.notes:
            print(f"    - {n}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
