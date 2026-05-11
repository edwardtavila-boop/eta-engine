"""
EVOLUTIONARY TRADING ALGO  //  scripts.tick_anomaly_detector
============================================================
Per-tick validator catching corrupt or anomalous trade prints
BEFORE they reach the strategy stack (mirror of
depth_anomaly_detector but for ticks).

Why this exists
---------------
Tick streams from IBKR can contain:
  - Zero-size prints (filled-or-cancel side effects, not real trades)
  - NaN / inf prices
  - Stale ticks marked unreported=true
  - Past-limit ticks marked past_limit=true (off-tick prints)
  - ts/epoch_s drift
  - Implausible price jumps (>10% in one tick from a real-tick baseline)

Each anomaly maps to a verdict:
  - OK         → consume the tick
  - WARN       → consume but log
  - SKIP       → strategy MUST skip this tick

Integration
-----------
Inline guard for strategies that consume the tick stream:

    result = validate_tick(tick, last_real_price=state.last_trade_price)
    if result.verdict == "SKIP":
        return  # don't update strategy state on bad tick

Batch mode for capture audit:
    python -m eta_engine.scripts.tick_anomaly_detector --symbol MNQ
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
LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TICK_ANOMALY_LOG = LOG_DIR / "tick_anomalies.jsonl"
TICKS_DIR = ROOT.parent / "mnq_data" / "ticks"


@dataclass
class TickValidationResult:
    verdict: str               # "OK" | "WARN" | "SKIP"
    anomalies: list[str] = field(default_factory=list)


def _is_finite_number(x: object) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v)


def validate_tick(tick: dict,
                   *, last_real_price: float | None = None,
                   max_price_jump_pct: float = 10.0,
                   max_ts_drift_seconds: float = 60.0) -> TickValidationResult:
    """Validate one tick.  Returns verdict + anomaly list.

    Strategies should treat SKIP as "don't update state on this
    tick" — equivalent to the depth_anomaly_detector SKIP verdict.
    """
    anomalies: list[str] = []
    verdict = "OK"

    # ── 1. Required fields ─────────────────────────────────────────
    for key in ("price",):
        if key not in tick:
            anomalies.append(f"missing_field:{key}")
    if anomalies:
        return TickValidationResult(verdict="SKIP", anomalies=anomalies)

    # ── 2. Price validity ──────────────────────────────────────────
    price = tick.get("price")
    if not _is_finite_number(price):
        anomalies.append(f"bad_price:{price}")
        return TickValidationResult(verdict="SKIP", anomalies=anomalies)
    if float(price) <= 0:
        anomalies.append(f"non_positive_price:{price}")
        return TickValidationResult(verdict="SKIP", anomalies=anomalies)

    # ── 3. Size validity (zero size is unusual; NaN is bad) ───────
    size = tick.get("size", 0)
    if not _is_finite_number(size):
        anomalies.append(f"bad_size:{size}")
        return TickValidationResult(verdict="SKIP", anomalies=anomalies)
    if float(size) < 0:
        anomalies.append(f"negative_size:{size}")
        return TickValidationResult(verdict="SKIP", anomalies=anomalies)
    if float(size) == 0:
        anomalies.append("zero_size")
        verdict = "WARN"

    # ── 4. Flag fields ─────────────────────────────────────────────
    if bool(tick.get("unreported", False)):
        anomalies.append("unreported_flag")
        verdict = "WARN"
    if bool(tick.get("past_limit", False)):
        anomalies.append("past_limit_flag")
        verdict = "WARN"

    # ── 5. Price jump check (vs last real price) ──────────────────
    if last_real_price is not None and last_real_price > 0:
        jump_pct = abs(float(price) - last_real_price) / last_real_price * 100
        if jump_pct > max_price_jump_pct:
            anomalies.append(
                f"implausible_jump:{jump_pct:.2f}%_from_{last_real_price}")
            return TickValidationResult(verdict="SKIP", anomalies=anomalies)

    # ── 6. ts vs epoch_s drift ────────────────────────────────────
    ts_str = tick.get("ts")
    epoch_s = tick.get("epoch_s")
    if isinstance(ts_str, str) and _is_finite_number(epoch_s):
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            drift = abs(dt.timestamp() - float(epoch_s))
            if drift > max_ts_drift_seconds:
                anomalies.append(f"ts_epoch_drift:{drift:.1f}s")
                verdict = "WARN"
        except ValueError:
            anomalies.append(f"bad_ts_format:{ts_str}")
            verdict = "WARN"

    return TickValidationResult(verdict=verdict, anomalies=anomalies)


def audit_tick_file(path: Path, *, max_emit: int = 0) -> dict:
    """Walk one day's tick file; emit per-tick verdicts.  Tracks last
    real price across ticks for jump detection."""
    summary: dict = {"path": str(path), "n_total": 0, "n_ok": 0,
                      "n_warn": 0, "n_skip": 0,
                      "anomaly_counts": {}}
    if not path.exists():
        summary["error"] = "file_not_found"
        return summary
    last_real_price: float | None = None
    emitted = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                summary["n_total"] += 1
                try:
                    tick = json.loads(line)
                except json.JSONDecodeError:
                    summary["n_skip"] += 1
                    summary["anomaly_counts"]["bad_json"] = \
                        summary["anomaly_counts"].get("bad_json", 0) + 1
                    continue
                result = validate_tick(tick, last_real_price=last_real_price)
                key = f"n_{result.verdict.lower()}"
                summary[key] = summary.get(key, 0) + 1
                for a in result.anomalies:
                    base_key = a.split(":", 1)[0]
                    summary["anomaly_counts"][base_key] = \
                        summary["anomaly_counts"].get(base_key, 0) + 1
                if result.verdict == "OK":
                    last_real_price = float(tick.get("price", 0))
                if result.verdict != "OK" and emitted < max_emit:
                    try:
                        with TICK_ANOMALY_LOG.open("a", encoding="utf-8") as out:
                            out.write(json.dumps({
                                "ts": datetime.now(UTC).isoformat(),
                                "source_file": str(path),
                                "tick_ts": tick.get("ts"),
                                "tick_price": tick.get("price"),
                                "verdict": result.verdict,
                                "anomalies": result.anomalies,
                            }, separators=(",", ":")) + "\n")
                        emitted += 1
                    except OSError as e:
                        print(f"WARN: tick anomaly log write failed: {e}",
                              file=sys.stderr)
    except OSError as e:
        summary["error"] = str(e)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="MNQ")
    ap.add_argument("--date", default=None)
    ap.add_argument("--max-emit", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    date_str = args.date or datetime.now(UTC).strftime("%Y%m%d")
    path = TICKS_DIR / f"{args.symbol}_{date_str}.jsonl"
    summary = audit_tick_file(path, max_emit=args.max_emit)

    if args.json:
        print(json.dumps(summary, indent=2))
        return 1 if summary.get("n_skip", 0) > summary.get("n_total", 0) * 0.05 else 0

    print()
    print("=" * 78)
    print(f"TICK ANOMALY AUDIT  ({args.symbol} {date_str})")
    print("=" * 78)
    print(f"  file        : {path}")
    if summary.get("error"):
        print(f"  ERROR       : {summary['error']}")
        return 1
    print(f"  n_total     : {summary['n_total']:,}")
    print(f"  n_ok        : {summary.get('n_ok', 0):,}")
    print(f"  n_warn      : {summary.get('n_warn', 0):,}")
    print(f"  n_skip      : {summary.get('n_skip', 0):,}")
    if summary.get("anomaly_counts"):
        print()
        print("  Anomaly breakdown:")
        for k, v in sorted(summary["anomaly_counts"].items(),
                            key=lambda kv: kv[1], reverse=True):
            print(f"    {k:<30s} {v:,}")
    print()
    return 1 if summary.get("n_skip", 0) > summary.get("n_total", 1) * 0.05 else 0


if __name__ == "__main__":
    raise SystemExit(main())
