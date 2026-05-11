"""
EVOLUTIONARY TRADING ALGO  //  scripts.capture_rotation
=======================================================
Compress + archive old tick + depth capture files to keep the
hot-data window small and disk usage predictable.

Why this exists
---------------
Phase-1 captures land as raw JSONL at ~1.2-5.6 GB/day.  Hot
analysis (the supercharge harness, bar reconstruction) only
needs the last ~14 days online; older history can be gzipped
(8-15× smaller) and rotated to cold storage.

This script:
1. Finds every ``mnq_data/ticks/<SYMBOL>_<YYYYMMDD>.jsonl`` and
   ``mnq_data/depth/<SYMBOL>_<YYYYMMDD>.jsonl`` older than
   ``--keep-days`` (default 14).
2. Gzips each to the same path with ``.gz`` suffix.
3. Verifies the .gz was written before deleting the .jsonl.
4. Optionally moves .gz files older than ``--cold-days`` (default
   90) to ``mnq_data/<kind>/cold/<YYYY>/<MM>/`` for further cold-
   storage rotation (S3 / external drive / etc).

Read-only by default — use ``--apply`` to actually compress + delete.

Run
---
::

    # Dry-run: show what would happen
    python -m eta_engine.scripts.capture_rotation

    # Actually compress + delete .jsonl files older than 14d
    python -m eta_engine.scripts.capture_rotation --apply

    # Custom retention
    python -m eta_engine.scripts.capture_rotation --apply \\
        --keep-days 7 --cold-days 60
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

LOG_DIR = ROOT.parent / "logs" / "eta_engine"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TICKS_DIR = ROOT.parent / "mnq_data" / "ticks"
DEPTH_DIR = ROOT.parent / "mnq_data" / "depth"
ROTATION_LOG = LOG_DIR / "capture_rotation.jsonl"


def _date_from_filename(p: Path) -> date | None:
    """Filename pattern: <SYMBOL>_<YYYYMMDD>.jsonl[.gz].  Extract date."""
    stem = p.stem if p.suffix == ".jsonl" else p.with_suffix("").stem  # strip .gz then .jsonl
    parts = stem.rsplit("_", 1)
    if len(parts) != 2:
        return None
    try:
        return datetime.strptime(parts[1], "%Y%m%d").date()
    except ValueError:
        return None


def _gzip_in_place(src: Path) -> Path:
    """Gzip src to src + '.gz', verifying the output is non-zero before
    returning the new path.  Caller decides whether to remove src."""
    dst = src.with_suffix(src.suffix + ".gz")  # foo.jsonl → foo.jsonl.gz
    with src.open("rb") as f_in, gzip.open(dst, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=64 * 1024)
    if not dst.exists() or dst.stat().st_size == 0:
        raise OSError(f"gzip wrote empty file: {dst}")
    return dst


def _process_kind(d: Path, kind: str, today: date, keep_days: int,
                  cold_days: int, *, apply: bool) -> dict:
    """Walk d, compress files older than keep_days, cold-archive files
    older than cold_days.  Return per-file outcomes."""
    if not d.exists():
        return {"kind": kind, "dir": str(d), "n_compressed": 0,
                "n_cold_archived": 0, "actions": [], "note": "dir missing"}

    actions: list[dict] = []
    n_compressed = 0
    n_cold = 0
    cold_root = d / "cold"

    for p in sorted(d.iterdir()):
        # Skip the cold subdir itself
        if p.is_dir():
            continue
        # Skip the helper scripts that live under mnq_data/history/
        if p.suffix not in {".jsonl", ".gz"}:
            continue
        file_date = _date_from_filename(p)
        if file_date is None:
            continue
        age_days = (today - file_date).days
        action = {"file": p.name, "age_days": age_days, "size_bytes": p.stat().st_size,
                  "ext": p.suffix, "outcome": "kept-hot"}

        if p.suffix == ".jsonl" and age_days > keep_days:
            # Compress
            if apply:
                try:
                    gz = _gzip_in_place(p)
                    p.unlink()
                    action["outcome"] = "compressed"
                    action["gz_size_bytes"] = gz.stat().st_size
                    action["compression_ratio"] = round(action["size_bytes"] / max(action["gz_size_bytes"], 1), 1)
                    n_compressed += 1
                except OSError as e:
                    action["outcome"] = f"compress-error:{e}"
            else:
                action["outcome"] = "would-compress"
        elif p.suffix == ".gz" and age_days > cold_days:
            # Move to cold/YYYY/MM/
            dest = cold_root / f"{file_date.year:04d}" / f"{file_date.month:02d}" / p.name
            if apply:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(dest))
                    action["outcome"] = "cold-archived"
                    action["cold_path"] = str(dest)
                    n_cold += 1
                except OSError as e:
                    action["outcome"] = f"cold-error:{e}"
            else:
                action["outcome"] = "would-cold-archive"

        actions.append(action)

    return {
        "kind": kind, "dir": str(d),
        "n_compressed": n_compressed, "n_cold_archived": n_cold,
        "n_files_total": len(actions),
        "actions": actions,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep-days", type=int, default=14,
                    help="Days to keep raw .jsonl hot (default 14)")
    ap.add_argument("--cold-days", type=int, default=90,
                    help="Days to keep .gz online before cold-archiving (default 90)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually compress + move (default: dry-run)")
    ap.add_argument("--json", action="store_true",
                    help="JSON output (machine-readable)")
    args = ap.parse_args()

    today = datetime.now(UTC).date()
    ticks = _process_kind(TICKS_DIR, "ticks", today, args.keep_days,
                          args.cold_days, apply=args.apply)
    depth = _process_kind(DEPTH_DIR, "depth", today, args.keep_days,
                          args.cold_days, apply=args.apply)

    digest = {
        "ts": datetime.now(UTC).isoformat(),
        "today": str(today),
        "keep_days": args.keep_days,
        "cold_days": args.cold_days,
        "apply": args.apply,
        "ticks": {k: v for k, v in ticks.items() if k != "actions"},
        "depth": {k: v for k, v in depth.items() if k != "actions"},
        "totals": {
            "n_compressed": ticks["n_compressed"] + depth["n_compressed"],
            "n_cold_archived": ticks["n_cold_archived"] + depth["n_cold_archived"],
        },
    }
    try:
        with ROTATION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(digest, separators=(",", ":")) + "\n")
    except OSError:
        pass

    if args.json:
        out = dict(digest)
        out["ticks_actions"] = ticks["actions"]
        out["depth_actions"] = depth["actions"]
        print(json.dumps(out, indent=2))
    else:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"capture-rotation: {mode}")
        print(f"  ticks dir: {ticks['n_files_total']} files, "
              f"{ticks['n_compressed']} {'compressed' if args.apply else 'would-compress'}, "
              f"{ticks['n_cold_archived']} {'archived' if args.apply else 'would-archive'}")
        print(f"  depth dir: {depth['n_files_total']} files, "
              f"{depth['n_compressed']} {'compressed' if args.apply else 'would-compress'}, "
              f"{depth['n_cold_archived']} {'archived' if args.apply else 'would-archive'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
