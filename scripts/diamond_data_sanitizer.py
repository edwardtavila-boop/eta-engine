"""
EVOLUTIONARY TRADING ALGO  //  scripts.diamond_data_sanitizer
=============================================================
Forward + backward quarantine for corrupt trade-close records.

Why this exists
---------------
The 2026-05-12 authenticity audit revealed that several diamond bots
have ledger records with implausible USD magnitudes (e.g.
eur_sweep_reclaim records show ~-$189,000 per trade on a 1-contract
position, ~91x the realistic max).  Root cause is upstream: the 6E
fill_price comes from a data source quoting the inverse-percentage
(98.43 instead of 1.08).  The point_value math is correct given the
fill_price; the fill_price is wrong.

Rather than chase the venue/data-source plumbing immediately (which
risks introducing a worse bug), this module sanitizes at the LEDGER
boundary:

  - Forward: every new close record is checked before it enters
    `closed_trade_ledger_latest.json`.  Implausible USD magnitudes
    are flagged + their USD column zeroed (R-multiples kept — they
    survive the scale bug, since realized_r is computed from stop
    distance and is dimension-free).

  - Backward: an idempotent re-tag pass over trade_closes.jsonl.
    Records with bad USD get an `_extra.quarantined_usd: true` flag
    and their `extra.realized_pnl` zeroed.  Original values are
    preserved in `_extra.quarantined_original_realized_pnl` for
    forensics + later un-quarantine if the operator fixes the feed.

What "implausible" means
------------------------
Per-trade USD magnitude > $5,000 on a 1-3 contract paper position
is the conservative threshold — well above the realistic max for
any of the diamond instruments (full CL stop ~= $5k, MNQ stop ~= $4).

The threshold matches diamond_authenticity_audit's scale-bug detector
so the two modules report consistent verdicts.

Output
------
- stdout / --json
- var/eta_engine/state/diamond_sanitizer_latest.json
- (if --apply-backward) writes back a sanitized copy of
  trade_closes.jsonl with a `.before-sanitize.bak` sidecar so the
  operator can undo

Run
---
::

    # Dry run — show what would be quarantined
    python -m eta_engine.scripts.diamond_data_sanitizer

    # Apply backward (rewrite trade_closes.jsonl with quarantine tags)
    python -m eta_engine.scripts.diamond_data_sanitizer --apply-backward
"""
from __future__ import annotations

# ruff: noqa: PLR2004
import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
STATE_DIR = WORKSPACE_ROOT / "var" / "eta_engine" / "state"
LEGACY_STATE_DIR = ROOT / "state"

#: Candidate paths for trade_closes (canonical + legacy)
TRADE_CLOSES_CANDIDATES = [
    STATE_DIR / "jarvis_intel" / "trade_closes.jsonl",
    LEGACY_STATE_DIR / "jarvis_intel" / "trade_closes.jsonl",
]

OUT_LATEST = STATE_DIR / "diamond_sanitizer_latest.json"

#: USD per-trade magnitude over this = scale bug → quarantine.
#: Matches diamond_authenticity_audit's threshold for consistency.
QUARANTINE_USD_THRESHOLD = 5_000.0


@dataclass
class SanitizerStats:
    path: str
    records_scanned: int = 0
    records_quarantined: int = 0
    records_already_quarantined: int = 0
    records_clean: int = 0
    bots_affected: list[str] = field(default_factory=list)
    sample_quarantined: list[dict] = field(default_factory=list)


def _record_is_corrupt(rec: dict) -> tuple[bool, float | None]:
    """Return (is_corrupt, observed_magnitude_usd)."""
    extra = rec.get("extra") or {}
    if not isinstance(extra, dict):
        return False, None
    pnl = extra.get("realized_pnl")
    if pnl is None:
        return False, None
    try:
        pnl_f = abs(float(pnl))
    except (TypeError, ValueError):
        return False, None
    qty = 1.0
    try:
        qty = max(abs(float(extra.get("qty") or 1.0)), 1.0)
    except (TypeError, ValueError):
        qty = 1.0
    per_contract = pnl_f / qty
    return per_contract > QUARANTINE_USD_THRESHOLD, pnl_f


def _quarantine_record(rec: dict) -> dict:
    """In-place quarantine: zero realized_pnl, preserve original under
    quarantined_original_realized_pnl, set quarantined_usd=True."""
    extra = rec.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        rec["extra"] = extra
    if extra.get("quarantined_usd"):
        return rec  # idempotent
    extra["quarantined_original_realized_pnl"] = extra.get("realized_pnl")
    extra["quarantined_usd"] = True
    extra["quarantined_at"] = datetime.now(UTC).isoformat()
    extra["quarantined_reason"] = (
        f"per-trade USD magnitude > ${QUARANTINE_USD_THRESHOLD:.0f} — "
        "implausible for paper futures (likely upstream feed scale bug)"
    )
    extra["realized_pnl"] = 0.0
    # Top-level convenience field for downstream consumers
    rec["_sanitizer_quarantined"] = True
    return rec


def scan_records(path: Path) -> SanitizerStats:
    """Pure read; never modifies the file."""
    stats = SanitizerStats(path=str(path))
    if not path.exists():
        return stats
    bots: set[str] = set()
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
                stats.records_scanned += 1
                extra = rec.get("extra") or {}
                if isinstance(extra, dict) and extra.get("quarantined_usd"):
                    stats.records_already_quarantined += 1
                    continue
                corrupt, mag = _record_is_corrupt(rec)
                if corrupt:
                    stats.records_quarantined += 1
                    bot_id = str(rec.get("bot_id") or "?")
                    bots.add(bot_id)
                    if len(stats.sample_quarantined) < 5:
                        stats.sample_quarantined.append({
                            "bot_id": bot_id,
                            "ts": rec.get("ts"),
                            "signal_id": rec.get("signal_id"),
                            "realized_pnl_magnitude": mag,
                        })
                else:
                    stats.records_clean += 1
    except OSError:
        pass
    stats.bots_affected = sorted(bots)
    return stats


def apply_backward(path: Path) -> SanitizerStats:
    """Rewrite the file with quarantine tags applied.  Creates a
    .before-sanitize.bak sidecar so the operator can undo."""
    stats = SanitizerStats(path=str(path))
    if not path.exists():
        return stats
    backup_path = path.with_suffix(path.suffix + ".before-sanitize.bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
    tmp_path = path.with_suffix(path.suffix + ".sanitize-tmp")
    bots: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as src, \
                tmp_path.open("w", encoding="utf-8") as dst:
            for line in src:
                raw = line.rstrip("\n")
                stripped = raw.strip()
                if not stripped:
                    dst.write(line)
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    dst.write(line)
                    continue
                stats.records_scanned += 1
                extra = rec.get("extra") or {}
                if isinstance(extra, dict) and extra.get("quarantined_usd"):
                    stats.records_already_quarantined += 1
                    dst.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    continue
                corrupt, _ = _record_is_corrupt(rec)
                if corrupt:
                    _quarantine_record(rec)
                    stats.records_quarantined += 1
                    bots.add(str(rec.get("bot_id") or "?"))
                else:
                    stats.records_clean += 1
                dst.write(json.dumps(rec, separators=(",", ":")) + "\n")
        # Atomic replace
        tmp_path.replace(path)
    except OSError as exc:
        print(f"WARN: sanitize backward failed: {exc}", file=sys.stderr)
        return stats
    stats.bots_affected = sorted(bots)
    return stats


def sanitize_forward(rec: dict) -> tuple[dict, bool]:
    """Forward sanitizer — called by the ledger writer BEFORE the
    record enters the ledger.  Returns (sanitized_rec, was_quarantined).

    Idempotent and side-effect-free on already-quarantined records.
    """
    extra = rec.get("extra") or {}
    if isinstance(extra, dict) and extra.get("quarantined_usd"):
        return rec, True
    corrupt, _ = _record_is_corrupt(rec)
    if corrupt:
        return _quarantine_record(rec), True
    return rec, False


def run() -> dict:
    by_path: list[SanitizerStats] = []
    for path in TRADE_CLOSES_CANDIDATES:
        if path.exists():
            by_path.append(scan_records(path))
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "quarantine_threshold_usd": QUARANTINE_USD_THRESHOLD,
        "paths": [asdict(s) for s in by_path],
        "total_records_scanned": sum(s.records_scanned for s in by_path),
        "total_records_to_quarantine": sum(s.records_quarantined for s in by_path),
        "total_records_already_quarantined": sum(
            s.records_already_quarantined for s in by_path),
        "total_records_clean": sum(s.records_clean for s in by_path),
        "bots_affected": sorted({
            b for s in by_path for b in s.bots_affected
        }),
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def apply_backward_all() -> dict:
    applied: list[SanitizerStats] = []
    for path in TRADE_CLOSES_CANDIDATES:
        if path.exists():
            applied.append(apply_backward(path))
    summary = {
        "ts": datetime.now(UTC).isoformat(),
        "mode": "apply_backward",
        "quarantine_threshold_usd": QUARANTINE_USD_THRESHOLD,
        "paths": [asdict(s) for s in applied],
        "total_records_scanned": sum(s.records_scanned for s in applied),
        "total_records_quarantined": sum(s.records_quarantined for s in applied),
        "total_records_already_quarantined": sum(
            s.records_already_quarantined for s in applied),
        "total_records_clean": sum(s.records_clean for s in applied),
        "bots_affected": sorted({
            b for s in applied for b in s.bots_affected
        }),
    }
    try:
        OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
        OUT_LATEST.write_text(
            json.dumps(summary, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        print(f"WARN: write_latest failed: {exc}", file=sys.stderr)
    return summary


def _print(summary: dict) -> None:
    print("=" * 100)
    print(
        f" DIAMOND DATA SANITIZER — {summary['ts']}  "
        f"(threshold ${summary['quarantine_threshold_usd']:.0f}/trade)",
    )
    print("=" * 100)
    print(f"  total scanned: {summary['total_records_scanned']}")
    if "total_records_to_quarantine" in summary:
        print(f"  would quarantine: {summary['total_records_to_quarantine']}")
    else:
        print(f"  quarantined: {summary['total_records_quarantined']}")
    print(f"  already-quarantined: {summary.get('total_records_already_quarantined', 0)}")
    print(f"  clean: {summary.get('total_records_clean', 0)}")
    print(f"  bots affected: {summary.get('bots_affected', [])}")
    for p in summary["paths"]:
        if p.get("sample_quarantined"):
            print(f"\n  Sample from {p['path']}:")
            for s in p["sample_quarantined"]:
                print(
                    f"    {s.get('bot_id'):28s}  pnl_mag=$"
                    f"{s.get('realized_pnl_magnitude') or 0:>12,.2f}  "
                    f"ts={s.get('ts')}",
                )
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply-backward", action="store_true",
                    help="Rewrite trade_closes.jsonl with quarantine tags")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    summary = (apply_backward_all() if args.apply_backward else run())
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
