"""Auto-clean old _archive*/ directories (Tier-3 #12, 2026-04-27).

Migration history is preserved in ``_archive*/`` directories
(``_archive_2026-04-25/``, ``_archive_2026-04-26/``, etc.) but eats
disk over time. This script:

  1. Finds all top-level ``_archive*/`` directories under the workspace
  2. For each one older than ``--max-age-days`` (default 90):
     a. Compresses the directory to ``.tar.gz``
     b. Optionally uploads to Backblaze B2 via Restic (if configured)
     c. Deletes the original directory

Run quarterly via scheduled task or manually after major migrations.
Idempotent: a directory that's already been archived is skipped.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("auto_archive_cleanup")

WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")


def parse_archive_date(name: str) -> datetime | None:
    """_archive_2026-04-25 -> datetime(2026, 4, 25, tzinfo=UTC)"""
    for prefix in ("_archive_", "_archive-"):
        if name.startswith(prefix):
            datestr = name[len(prefix) :]
            for fmt in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    return datetime.strptime(datestr, fmt).replace(tzinfo=UTC)
                except ValueError:
                    continue
    return None


def compress_dir(src: Path, dst: Path) -> int:
    """Tar+gzip src into dst. Returns total file count compressed."""
    count = 0
    with tarfile.open(dst, "w:gz") as tar:
        for p in src.rglob("*"):
            if p.is_file():
                tar.add(str(p), arcname=str(p.relative_to(src.parent)))
                count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", type=Path, default=WORKSPACE)
    p.add_argument("--max-age-days", type=int, default=90)
    p.add_argument("--archive-out", type=Path, default=WORKSPACE / "data" / "compressed_archives")
    p.add_argument(
        "--restic",
        action="store_true",
        help="After compressing, run `restic backup` on the .tar.gz (requires RESTIC_REPOSITORY + RESTIC_PASSWORD env)",
    )
    p.add_argument("--keep-uncompressed", action="store_true", help="Compress but do NOT delete the source directory")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not args.workspace.exists():
        logger.error("workspace not found: %s", args.workspace)
        return 1

    args.archive_out.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(UTC).timestamp() - (args.max_age_days * 86400)

    candidates = []
    for child in args.workspace.iterdir():
        if not child.is_dir():
            continue
        if not child.name.startswith("_archive"):
            continue
        d = parse_archive_date(child.name)
        if d is None:
            logger.debug("skip %s: name doesn't parse as a date", child.name)
            continue
        if d.timestamp() > cutoff:
            logger.debug(
                "skip %s: only %.0f days old (< %d)", child.name, (datetime.now(UTC) - d).days, args.max_age_days
            )
            continue
        candidates.append(child)

    if not candidates:
        logger.info("no archives older than %d days to clean", args.max_age_days)
        return 0

    logger.info("found %d archive(s) to compress + remove", len(candidates))

    for src in candidates:
        out = args.archive_out / f"{src.name}.tar.gz"
        if out.exists():
            logger.info("skip %s -- compressed copy already at %s", src.name, out)
            if not args.keep_uncompressed and not args.dry_run:
                shutil.rmtree(src, ignore_errors=True)
                logger.info("removed source: %s", src)
            continue

        logger.info("compressing %s -> %s", src.name, out.name)
        if args.dry_run:
            logger.info("(dry-run) would compress + delete")
            continue

        t0 = time.time()
        try:
            n = compress_dir(src, out)
        except (OSError, tarfile.TarError) as exc:
            logger.error("compress failed for %s: %s", src.name, exc)
            continue
        size_mb = out.stat().st_size / (1024 * 1024)
        logger.info("  -> %d files, %.1f MB, %.1fs", n, size_mb, time.time() - t0)

        if args.restic:
            import subprocess

            try:
                subprocess.run(
                    ["restic", "backup", str(out), "--tag", "archive"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                logger.info("  -> restic backup OK")
            except (subprocess.SubprocessError, OSError) as exc:
                logger.error("  -> restic backup FAILED: %s", exc)

        if not args.keep_uncompressed:
            shutil.rmtree(src, ignore_errors=True)
            logger.info("  -> removed source: %s", src)

    return 0


if __name__ == "__main__":
    sys.exit(main())
