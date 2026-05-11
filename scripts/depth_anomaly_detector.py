"""
EVOLUTIONARY TRADING ALGO  //  scripts.depth_anomaly_detector
=============================================================
Per-snapshot validator that catches corrupt or anomalous depth
data BEFORE it reaches the L2 strategy stack.

Why this exists
---------------
The L2 strategies (book_imbalance, microprice_drift, footprint) all
trust the depth snapshot schema implicitly.  When a snapshot is
corrupted — bid/ask crossed, qty=0, missing levels, NaN prices,
duplicated levels, mid not bracketed by NBBO — the strategies emit
phantom signals.  Zero-side fail-closed (I8 from the hardening
pass) catches *one* class of anomaly; this module catches the rest:

  - Crossed book (best_bid >= best_ask)
  - NaN / inf prices
  - Mid outside [best_bid, best_ask]
  - Spread negative or wildly off (vs |best_ask - best_bid|)
  - Duplicate prices in same side
  - Non-monotonic prices (bids not descending, asks not ascending)
  - Stale snapshot (epoch_s wildly different from ts)
  - Missing required fields

Each anomaly type maps to a verdict severity:
  - OK         → use the snapshot
  - WARN       → use but log
  - SKIP       → strategy MUST skip this snapshot
  - FAIL_CLOSE → strategy treats next N snaps as missing

How callers integrate
---------------------
Two integration modes:

1. Inline guard inside a strategy:
       result = validate_snapshot(snap)
       if result.verdict in {"SKIP", "FAIL_CLOSE"}:
           return None  # don't emit a signal

2. Post-hoc batch audit of capture files (after-the-fact diagnosis):
       python -m eta_engine.scripts.depth_anomaly_detector \\
           --symbol MNQ --date 20260511

The batch mode reads a day's depth file and emits a per-snap
verdict log to ``logs/eta_engine/depth_anomalies.jsonl``.
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ANOMALY_LOG = LOG_DIR / "depth_anomalies.jsonl"
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"


@dataclass
class ValidationResult:
    verdict: str               # "OK" | "WARN" | "SKIP" | "FAIL_CLOSE"
    anomalies: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def _is_finite_number(x: object) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v)


def validate_snapshot(snap: dict,
                       *, max_ts_drift_seconds: float = 60.0,
                       max_spread_inconsistency_pct: float = 0.20) -> ValidationResult:
    """Validate a depth snapshot.  Returns a ValidationResult.

    The strategy/overlay caller decides what to do based on verdict.
    SKIP = drop this snap; FAIL_CLOSE = treat next ~3 snaps as missing
    so the strategy state machine fully resets.
    """
    anomalies: list[str] = []
    details: dict = {}
    verdict = "OK"

    # ── 1. Required fields ─────────────────────────────────────────
    for key in ("bids", "asks", "spread", "mid"):
        if key not in snap:
            anomalies.append(f"missing_field:{key}")
    if anomalies:
        return ValidationResult(verdict="FAIL_CLOSE",
                                  anomalies=anomalies, details=details)

    bids = snap.get("bids", [])
    asks = snap.get("asks", [])

    # ── 2. Empty sides ─────────────────────────────────────────────
    if not bids and not asks:
        anomalies.append("both_sides_empty")
        return ValidationResult(verdict="FAIL_CLOSE",
                                  anomalies=anomalies, details=details)
    if not bids:
        anomalies.append("empty_bids")
        return ValidationResult(verdict="SKIP",
                                  anomalies=anomalies, details=details)
    if not asks:
        anomalies.append("empty_asks")
        return ValidationResult(verdict="SKIP",
                                  anomalies=anomalies, details=details)

    # ── 3. Each level structurally valid ──────────────────────────
    for side_name, levels in (("bids", bids), ("asks", asks)):
        for i, lv in enumerate(levels):
            if not isinstance(lv, dict):
                anomalies.append(f"{side_name}[{i}]_not_dict")
                continue
            price = lv.get("price")
            size = lv.get("size")
            if not _is_finite_number(price):
                anomalies.append(f"{side_name}[{i}]_bad_price:{price}")
            if not _is_finite_number(size):
                anomalies.append(f"{side_name}[{i}]_bad_size:{size}")
            elif float(size) < 0:
                anomalies.append(f"{side_name}[{i}]_negative_size:{size}")
    if any(a.startswith(("bids[", "asks[")) for a in anomalies):
        return ValidationResult(verdict="SKIP",
                                  anomalies=anomalies, details=details)

    # ── 4. NBBO sanity ─────────────────────────────────────────────
    best_bid = float(bids[0].get("price", 0))
    best_ask = float(asks[0].get("price", 0))
    mid = snap.get("mid")
    spread = snap.get("spread")

    if not _is_finite_number(mid):
        anomalies.append(f"bad_mid:{mid}")
        return ValidationResult(verdict="SKIP",
                                  anomalies=anomalies, details=details)
    if not _is_finite_number(spread):
        anomalies.append(f"bad_spread:{spread}")
        return ValidationResult(verdict="SKIP",
                                  anomalies=anomalies, details=details)

    mid_f = float(mid)
    spread_f = float(spread)

    # Crossed book
    if best_bid >= best_ask:
        anomalies.append(f"crossed_book:bid={best_bid}_ask={best_ask}")
        details["best_bid"] = best_bid
        details["best_ask"] = best_ask
        return ValidationResult(verdict="SKIP",
                                  anomalies=anomalies, details=details)

    # Mid not between NBBO
    if not (best_bid <= mid_f <= best_ask):
        anomalies.append(f"mid_outside_nbbo:{mid_f}_not_in_[{best_bid},{best_ask}]")
        verdict = "WARN"

    # Spread inconsistent with NBBO
    nbbo_spread = best_ask - best_bid
    if nbbo_spread > 0:
        ratio = abs(spread_f - nbbo_spread) / nbbo_spread
        if ratio > max_spread_inconsistency_pct:
            anomalies.append(
                f"spread_inconsistent:reported={spread_f}_nbbo={nbbo_spread}"
                f"_ratio={ratio:.2f}")
            verdict = "WARN"

    if spread_f < 0:
        anomalies.append(f"negative_spread:{spread_f}")
        return ValidationResult(verdict="SKIP",
                                  anomalies=anomalies, details=details)

    # ── 5. Monotonic prices within each side ──────────────────────
    bid_prices = [float(lv.get("price", 0)) for lv in bids]
    for i in range(1, len(bid_prices)):
        if bid_prices[i] >= bid_prices[i - 1]:
            anomalies.append(f"bids_not_descending_at_{i}")
            verdict = "WARN"
            break
    ask_prices = [float(lv.get("price", 0)) for lv in asks]
    for i in range(1, len(ask_prices)):
        if ask_prices[i] <= ask_prices[i - 1]:
            anomalies.append(f"asks_not_ascending_at_{i}")
            verdict = "WARN"
            break

    # ── 6. Duplicate prices within a side ─────────────────────────
    if len(set(bid_prices)) != len(bid_prices):
        anomalies.append("duplicate_bid_prices")
        verdict = "WARN"
    if len(set(ask_prices)) != len(ask_prices):
        anomalies.append("duplicate_ask_prices")
        verdict = "WARN"

    # ── 7. ts vs epoch_s drift ────────────────────────────────────
    ts_str = snap.get("ts")
    epoch_s = snap.get("epoch_s")
    if isinstance(ts_str, str) and _is_finite_number(epoch_s):
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            drift = abs(dt.timestamp() - float(epoch_s))
            if drift > max_ts_drift_seconds:
                anomalies.append(f"ts_epoch_drift:{drift:.1f}s")
                verdict = "WARN"
                details["ts_epoch_drift"] = round(drift, 2)
        except ValueError:
            anomalies.append(f"bad_ts_format:{ts_str}")
            verdict = "WARN"

    details["n_anomalies"] = len(anomalies)
    return ValidationResult(verdict=verdict, anomalies=anomalies, details=details)


def audit_capture_file(path: Path, *, max_emit: int = 0) -> dict:
    """Walk one day's depth file; emit per-snap verdicts.

    Returns aggregate summary {n_total, n_ok, n_warn, n_skip,
    n_fail_close, anomaly_counts}.  When max_emit > 0, also writes
    first N anomalous records to ANOMALY_LOG.
    """
    summary: dict = {"path": str(path), "n_total": 0,
                      "n_ok": 0, "n_warn": 0,
                      "n_skip": 0, "n_fail_close": 0,
                      "anomaly_counts": {}}
    if not path.exists():
        summary["error"] = "file_not_found"
        return summary
    emitted = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                summary["n_total"] += 1
                try:
                    snap = json.loads(line)
                except json.JSONDecodeError:
                    summary["n_fail_close"] += 1
                    summary["anomaly_counts"]["bad_json"] = \
                        summary["anomaly_counts"].get("bad_json", 0) + 1
                    continue
                result = validate_snapshot(snap)
                verdict_key = f"n_{result.verdict.lower()}"
                summary[verdict_key] = summary.get(verdict_key, 0) + 1
                for a in result.anomalies:
                    # Strip indices for grouping (e.g. "bids[3]_bad_price" → "bids_bad_price")
                    key = a.split(":", 1)[0]
                    summary["anomaly_counts"][key] = \
                        summary["anomaly_counts"].get(key, 0) + 1
                # Emit detailed record for first N anomalies
                if result.verdict != "OK" and emitted < max_emit:
                    try:
                        with ANOMALY_LOG.open("a", encoding="utf-8") as out:
                            out.write(json.dumps({
                                "ts": datetime.now(UTC).isoformat(),
                                "source_file": str(path),
                                "snap_ts": snap.get("ts"),
                                "verdict": result.verdict,
                                "anomalies": result.anomalies,
                                "details": result.details,
                            }, separators=(",", ":")) + "\n")
                        emitted += 1
                    except OSError as e:
                        print(f"WARN: anomaly log write failed: {e}",
                              file=sys.stderr)
    except OSError as e:
        summary["error"] = str(e)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--date", default=None,
                    help="YYYYMMDD; default = today")
    ap.add_argument("--max-emit", type=int, default=50,
                    help="Max anomalous records to write to anomaly log "
                    "(default 50; 0 = aggregate-only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    date_str = args.date or datetime.now(UTC).strftime("%Y%m%d")
    path = DEPTH_DIR / f"{args.symbol}_{date_str}.jsonl"
    summary = audit_capture_file(path, max_emit=args.max_emit)

    if args.json:
        print(json.dumps(summary, indent=2))
        # Exit 1 if any FAIL_CLOSE or significant SKIP rate
        if summary.get("n_fail_close", 0) > 0:
            return 1
        if summary.get("n_total", 0) > 0:
            skip_pct = summary.get("n_skip", 0) / summary["n_total"]
            if skip_pct > 0.05:  # >5% skip rate
                return 1
        return 0

    print()
    print("=" * 78)
    print(f"DEPTH ANOMALY AUDIT  ({args.symbol} {date_str})")
    print("=" * 78)
    print(f"  file        : {path}")
    if summary.get("error"):
        print(f"  ERROR       : {summary['error']}")
        return 1
    print(f"  n_total     : {summary['n_total']:,}")
    print(f"  n_ok        : {summary.get('n_ok', 0):,}")
    print(f"  n_warn      : {summary.get('n_warn', 0):,}")
    print(f"  n_skip      : {summary.get('n_skip', 0):,}")
    print(f"  n_fail_close: {summary.get('n_fail_close', 0):,}")
    print()
    if summary.get("anomaly_counts"):
        print("  Anomaly breakdown:")
        for k, v in sorted(summary["anomaly_counts"].items(),
                            key=lambda kv: kv[1], reverse=True):
            print(f"    {k:<40s} {v:,}")
    print()
    if summary.get("n_fail_close", 0) > 0:
        return 1
    if summary.get("n_total", 0) > 0:
        skip_pct = summary.get("n_skip", 0) / summary["n_total"]
        if skip_pct > 0.05:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
