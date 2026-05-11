"""
EVOLUTIONARY TRADING ALGO  //  scripts.disk_space_monitor
=========================================================
VPS disk-space watchdog for the Phase-1 tick + depth capture
daemons.

Why this exists
---------------
Per docs/PHASE1_CAPTURE_SETUP.md, expected capture volume is
1.2-5.6 GB/day for 8 symbols.  At 5 GB/day, a 500 GB free
partition lasts ~100 days before silently filling.  Once disk
fills, capture daemons error every write and we silently lose
data the same way as if the daemons had crashed.

This monitor checks free space on the disk hosting the capture
output dirs (``mnq_data/ticks/``, ``mnq_data/depth/``) and on the
log dir (``logs/eta_engine/``).  Emits YELLOW at 30 GB free,
RED at 10 GB, CRITICAL at 2 GB.

Output
------
* JSONL append to logs/eta_engine/disk_space.jsonl
* Alert append to logs/eta_engine/alerts_log.jsonl on YELLOW or worse

Run
---
::

    python -m eta_engine.scripts.disk_space_monitor
    python -m eta_engine.scripts.disk_space_monitor --json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TICKS_DIR = ROOT.parent / "mnq_data" / "ticks"
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"
HISTORY_LOG = LOG_DIR / "disk_space.jsonl"
ALERT_LOG = LOG_DIR / "alerts_log.jsonl"

GB = 1024 ** 3
THRESHOLDS = {
    # free_gb_floor → verdict (worst-first ordering)
    2.0:  "CRITICAL",   # ≤2 GB — capture will fail any moment
    10.0: "RED",        # ≤10 GB — ~2 days of capture left
    30.0: "YELLOW",     # ≤30 GB — ~6 days runway, plan rotation
}


def _verdict_for(free_gb: float) -> str:
    for floor in sorted(THRESHOLDS.keys()):
        if free_gb <= floor:
            return THRESHOLDS[floor]
    return "GREEN"


def _stat_one(label: str, path: Path) -> dict:
    """Return free/used/total for the partition hosting `path`.
    Falls back gracefully if the path doesn't exist."""
    target = path if path.exists() else path.parent
    if not target.exists():
        target = ROOT.parent  # last resort: workspace root
    try:
        usage = shutil.disk_usage(target)
    except OSError as e:
        return {"label": label, "path": str(path), "error": str(e),
                "verdict": "ERROR"}
    free_gb = usage.free / GB
    used_gb = usage.used / GB
    total_gb = usage.total / GB
    return {
        "label": label,
        "path": str(target),
        "free_gb": round(free_gb, 2),
        "used_gb": round(used_gb, 2),
        "total_gb": round(total_gb, 2),
        "pct_used": round(100.0 * usage.used / usage.total, 1),
        "verdict": _verdict_for(free_gb),
    }


def _emit_alert(level: str, message: str, payload: dict) -> None:
    record = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source": "disk_space_monitor",
        "level": level,
        "message": message,
        "payload": payload,
    }
    try:
        with ALERT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as e:
        # D6: surface to stderr so cron captures the failure.  Silent
        # swallow was the original behaviour and meant disk-full
        # incidents went un-recorded when the alert log itself
        # couldn't be written.
        print(f"disk_space_monitor WARN: could not append alert to "
              f"{ALERT_LOG}: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="JSON output (machine-readable)")
    args = ap.parse_args()

    checks = [
        _stat_one("ticks", TICKS_DIR),
        _stat_one("depth", DEPTH_DIR),
        _stat_one("logs",  LOG_DIR),
    ]

    # Rolling worst verdict across all monitored partitions
    rank = {"GREEN": 0, "YELLOW": 1, "RED": 2, "CRITICAL": 3, "ERROR": 4}
    worst = max(checks, key=lambda c: rank.get(c.get("verdict", "ERROR"), -1))
    overall = worst.get("verdict", "ERROR")

    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "verdict": overall,
        "worst_partition": worst.get("path"),
        "checks": checks,
    }
    try:
        with HISTORY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError as e:
        print(f"disk_space_monitor WARN: could not append digest to "
              f"{HISTORY_LOG}: {e}", file=sys.stderr)

    if overall != "GREEN":
        _emit_alert(
            overall,
            f"disk space {overall}: worst partition {worst.get('path')} "
            f"has {worst.get('free_gb')} GB free",
            digest,
        )

    if args.json:
        print(json.dumps(digest, indent=2))
    else:
        print(f"disk-space: {overall}")
        for c in checks:
            v = c.get("verdict", "?")
            free = c.get("free_gb", "?")
            total = c.get("total_gb", "?")
            print(f"  {c['label']:<6s} {v:<8s} {free} / {total} GB free  ({c.get('pct_used', '?')}% used)")
            if c.get("error"):
                print(f"         error: {c['error']}")

    # Exit 0 GREEN, 1 YELLOW, 2 RED, 3 CRITICAL
    return {"GREEN": 0, "YELLOW": 1, "RED": 2, "CRITICAL": 3, "ERROR": 4}.get(overall, 4)


if __name__ == "__main__":
    raise SystemExit(main())
