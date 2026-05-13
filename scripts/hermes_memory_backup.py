"""
Hermes operator-memory store backup.

Live SQLite backup of ``hermes_memory_store.db`` via the online-backup
API (``Connection.backup()``). Safe to run while Hermes Agent is reading
or writing — SQLite's backup API guarantees a consistent snapshot
without blocking writes.

Why a separate backup script (not a cron in Hermes itself):

  * Hermes's `holographic` plugin doesn't ship a backup hook.
  * Operator memory is irreplaceable — a corrupt DB or accidental
    `DELETE FROM facts` would erase weeks of context.
  * Backups belong on disk separate from the active DB so a single
    filesystem fault can't take both. We write to
    ``var/eta_engine/state/backups/hermes_memory/`` and keep a rolling
    window of the last ``KEEP_LAST`` backups (default 14 = two weeks
    of nightly backups).

Usage:

    python -m eta_engine.scripts.hermes_memory_backup
        [--src <path>] [--dest <dir>] [--keep <n>] [--quiet]

Default schedule: nightly at 04:00 UTC via Windows Task Scheduler.
Registration helper sits at ``deploy/register_memory_backup_task.ps1``.

Restore: just copy a backup file back to the active path, then restart
the Hermes gateway. Each backup is a complete standalone SQLite DB.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("eta_engine.scripts.hermes_memory_backup")

DEFAULT_SRC = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_memory_store.db",
)
DEFAULT_DEST_DIR = Path(
    r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\backups\hermes_memory",
)
DEFAULT_KEEP_LAST = 14


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def backup_db(src: Path, dest_dir: Path) -> Path | None:
    """Snapshot ``src`` to ``dest_dir/hermes_memory_<stamp>.db`` via SQLite
    online-backup API. Returns the destination path on success, ``None``
    on failure (logged, not raised).
    """
    if not src.exists():
        logger.warning("memory backup: source missing at %s — skipping", src)
        return None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("memory backup: cannot create dest dir %s: %s", dest_dir, exc)
        return None

    dest = dest_dir / f"hermes_memory_{_stamp()}.db"
    src_conn: sqlite3.Connection | None = None
    dest_conn: sqlite3.Connection | None = None
    try:
        # Open source READ-ONLY via URI so we never accidentally write.
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
        dest_conn = sqlite3.connect(str(dest), timeout=30.0)
        # `.backup()` uses SQLite's online backup API — safe under writes.
        src_conn.backup(dest_conn)
        logger.info("memory backup: %s -> %s", src, dest)
        return dest
    except sqlite3.Error as exc:
        logger.error("memory backup: sqlite error: %s", exc)
        # Clean up a partial dest file so we don't leave half-snapshots
        with contextlib.suppress(OSError):
            dest.unlink()
        return None
    finally:
        if src_conn is not None:
            with contextlib.suppress(sqlite3.Error):
                src_conn.close()
        if dest_conn is not None:
            with contextlib.suppress(sqlite3.Error):
                dest_conn.close()


def prune_old_backups(dest_dir: Path, keep_last: int) -> list[Path]:
    """Keep only the newest ``keep_last`` backups in ``dest_dir``.
    Returns a list of deleted file paths.
    """
    if keep_last <= 0:
        return []
    if not dest_dir.exists():
        return []
    backups = sorted(
        dest_dir.glob("hermes_memory_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    to_delete = backups[keep_last:]
    deleted: list[Path] = []
    for p in to_delete:
        try:
            p.unlink()
            deleted.append(p)
        except OSError as exc:
            logger.warning("memory backup: could not delete old %s: %s", p, exc)
    if deleted:
        logger.info("memory backup: pruned %d old backup(s)", len(deleted))
    return deleted


def verify_backup(path: Path) -> bool:
    """Open the backup file and run a quick integrity_check.

    Returns True iff PRAGMA integrity_check returns 'ok'. Logs failure
    detail at WARNING level; never raises.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        logger.warning("memory backup verify: cannot open %s: %s", path, exc)
        return False
    try:
        cur = conn.execute("PRAGMA integrity_check")
        row = cur.fetchone()
        ok = bool(row) and row[0] == "ok"
        if not ok:
            logger.warning("memory backup verify: integrity_check FAILED on %s: %s", path, row)
        return ok
    except sqlite3.Error as exc:
        logger.warning("memory backup verify: query failed: %s", exc)
        return False
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def run(
    src: Path = DEFAULT_SRC,
    dest_dir: Path = DEFAULT_DEST_DIR,
    keep_last: int = DEFAULT_KEEP_LAST,
    verify: bool = True,
) -> dict[str, object]:
    """Run one backup cycle. Returns a summary dict for callers (or stdout).

    Summary keys:
      * status: "ok" | "skipped" | "failed" | "verify_failed"
      * backup_path: path written (None if no backup)
      * pruned: count of deleted old backups
      * src_size_bytes / backup_size_bytes (when applicable)
    """
    summary: dict[str, object] = {
        "status": "failed",
        "backup_path": None,
        "pruned": 0,
        "src_size_bytes": None,
        "backup_size_bytes": None,
    }
    if not src.exists():
        summary["status"] = "skipped"
        return summary
    summary["src_size_bytes"] = src.stat().st_size

    dest = backup_db(src, dest_dir)
    if dest is None:
        return summary
    summary["backup_path"] = str(dest)
    with contextlib.suppress(OSError):
        summary["backup_size_bytes"] = dest.stat().st_size

    if verify and not verify_backup(dest):
        summary["status"] = "verify_failed"
        # Don't keep a corrupt backup
        with contextlib.suppress(OSError):
            dest.unlink()
        summary["backup_path"] = None
        return summary

    deleted = prune_old_backups(dest_dir, keep_last)
    summary["pruned"] = len(deleted)
    summary["status"] = "ok"
    return summary


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Snapshot the Hermes operator-memory SQLite store.",
    )
    p.add_argument("--src", type=Path, default=DEFAULT_SRC)
    p.add_argument("--dest", type=Path, default=DEFAULT_DEST_DIR)
    p.add_argument("--keep", type=int, default=DEFAULT_KEEP_LAST)
    p.add_argument("--no-verify", action="store_true", help="Skip integrity_check on the new backup")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    summary = run(
        src=args.src,
        dest_dir=args.dest,
        keep_last=args.keep,
        verify=not args.no_verify,
    )
    # Pretty single-line summary, machine-parseable
    print(
        f"status={summary['status']} "
        f"backup={summary['backup_path']} "
        f"src_bytes={summary['src_size_bytes']} "
        f"backup_bytes={summary['backup_size_bytes']} "
        f"pruned={summary['pruned']}"
    )
    if summary["status"] in ("ok", "skipped"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
