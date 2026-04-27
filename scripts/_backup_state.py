"""Rotating-snapshot backup + integrity check for critical state files.

Without this, a corrupted or accidentally-truncated state file is
unrecoverable. Git history protects committed files only -- state files
that are written every minute (alerts_log.jsonl, baselines, etc.) live
between commits and are at the mercy of partial-write bugs.

What gets backed up
-------------------
``CRITICAL_FILES`` below. Each is copied to
``docs/_backups/<basename>.<UTC-timestamp>.bak`` on every run. The
script keeps the most recent ``--keep`` snapshots (default 14) per
file and prunes older ones.

Integrity check
---------------
After taking the new snapshot, the script verifies every viable
snapshot in the backup directory:

* file size > 0
* JSON files: parses as JSON
* JSONL files: every non-empty line parses as JSON

Then computes the count of viable snapshots per critical file.

Verdict
-------
* GREEN  -- every critical file has >= ``--min-snapshots`` viable copies
            AND the freshest snapshot is < ``--max-stale-h`` old
* YELLOW -- one critical file is short on snapshots OR latest is stale
* RED    -- two or more shortages OR a critical file failed integrity

Exit codes
----------
0 GREEN, 1 YELLOW, 2 RED, 9 backup directory unwritable
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = ROOT / "docs" / "_backups"

CRITICAL_FILES = [
    ROOT / "docs" / "alerts_log.jsonl",
    ROOT / "docs" / "decisions_v1.json",
    ROOT / "docs" / "kill_log.json",
    ROOT / "docs" / "sharpe_baseline.json",
    ROOT / "docs" / "cross_regime" / "cross_regime_validation.json",
    ROOT / "docs" / "cross_regime" / "regime_exclusions.json",
]


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _validate_snapshot(p: Path) -> tuple[bool, str]:
    """Return (is_viable, reason_if_not)."""
    if not p.exists():
        return (False, "missing")
    if p.stat().st_size == 0:
        return (False, "zero-size")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return (False, f"read-error: {e}")
    if p.name.endswith(".json.bak") or ".json." in p.name:
        try:
            json.loads(text)
        except (ValueError, TypeError) as e:
            return (False, f"json-parse: {e}")
    elif p.name.endswith(".jsonl.bak") or ".jsonl." in p.name:
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except (ValueError, TypeError) as e:
                return (False, f"jsonl-parse line {i + 1}: {e}")
    return (True, "ok")


def _snapshot_one(src: Path, backup_dir: Path) -> tuple[Path | None, str]:
    if not src.exists():
        return (None, f"source missing: {src}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()
    dest = backup_dir / f"{src.name}.{stamp}.bak"
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        return (None, f"copy failed: {e}")
    return (dest, "ok")


def _existing_snapshots(backup_dir: Path, basename: str) -> list[Path]:
    if not backup_dir.exists():
        return []
    out = sorted(backup_dir.glob(f"{basename}.*.bak"))
    return out


def _prune(backup_dir: Path, basename: str, keep: int) -> int:
    snaps = _existing_snapshots(backup_dir, basename)
    if len(snaps) <= keep:
        return 0
    to_remove = snaps[:-keep]
    pruned = 0
    for p in to_remove:
        try:
            p.unlink()
            pruned += 1
        except OSError:
            continue
    return pruned


def _stamp_from_name(p: Path) -> float | None:
    """Extract embedded UTC timestamp from filename like ``foo.20260417T200012Z.bak``."""
    parts = p.name.split(".")
    for tok in parts:
        if len(tok) == 16 and tok.endswith("Z") and "T" in tok:
            try:
                return (
                    datetime.strptime(tok, "%Y%m%dT%H%M%SZ")
                    .replace(
                        tzinfo=UTC,
                    )
                    .timestamp()
                )
            except ValueError:
                continue
    return None


def _evaluate(backup_dir: Path, min_snaps: int, max_stale_h: float) -> tuple[str, list[str]]:
    issues: list[str] = []
    now_ts = datetime.now(UTC).timestamp()
    severity = 0  # 0=GREEN, 1=YELLOW, 2=RED
    for src in CRITICAL_FILES:
        snaps = _existing_snapshots(backup_dir, src.name)
        viable = []
        for s in snaps:
            ok, reason = _validate_snapshot(s)
            if ok:
                viable.append(s)
            else:
                issues.append(f"  {src.name}: snapshot {s.name} -- {reason}")
                severity = max(severity, 2)
        if len(viable) < min_snaps:
            issues.append(
                f"  {src.name}: only {len(viable)} viable snapshots (min {min_snaps})",
            )
            severity = max(severity, 1)
        if viable:
            latest = viable[-1]
            # Prefer embedded timestamp over mtime (copy2 preserves source mtime)
            stamp_ts = _stamp_from_name(latest)
            ref_ts = stamp_ts if stamp_ts is not None else latest.stat().st_mtime
            age_h = (now_ts - ref_ts) / 3600.0
            if age_h > max_stale_h:
                issues.append(
                    f"  {src.name}: freshest snapshot {age_h:.1f}h old (cap {max_stale_h:.0f}h)",
                )
                severity = max(severity, 1)
    overall = ("GREEN", "YELLOW", "RED")[severity]
    return overall, issues


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    p.add_argument("--keep", type=int, default=14, help="snapshots per file to retain")
    p.add_argument("--min-snapshots", type=int, default=7)
    p.add_argument("--max-stale-h", type=float, default=25.0)
    p.add_argument("--no-snapshot", action="store_true", help="check only, don't take new snapshots")
    args = p.parse_args(argv)

    try:
        args.backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"backup-state: data-missing -- backup dir unwritable: {e}")
        return 9

    fresh_count = 0
    pruned_count = 0
    if not args.no_snapshot:
        for src in CRITICAL_FILES:
            dest, msg = _snapshot_one(src, args.backup_dir)
            if dest is None:
                print(f"backup-state: skipping {src.name} -- {msg}")
                continue
            fresh_count += 1
            pruned_count += _prune(args.backup_dir, src.name, args.keep)

    overall, issues = _evaluate(args.backup_dir, args.min_snapshots, args.max_stale_h)
    print(
        f"backup-state: {overall} -- snapshotted={fresh_count}, "
        f"pruned={pruned_count}, files-tracked={len(CRITICAL_FILES)}",
    )
    if issues:
        for line in issues:
            print(line)
    return {"GREEN": 0, "YELLOW": 1, "RED": 2}[overall]


if __name__ == "__main__":
    sys.exit(main())
