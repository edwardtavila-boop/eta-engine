"""Tests for hermes_memory_backup — SQLite online-backup script."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _seed_db(path: Path, rows: int = 3) -> None:
    """Create a tiny SQLite DB with a few rows for backup testing."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT)")
        for i in range(rows):
            conn.execute("INSERT INTO facts(subject) VALUES (?)", (f"sub_{i}",))
        conn.commit()
    finally:
        conn.close()


def test_backup_writes_consistent_snapshot(tmp_path: Path) -> None:
    """A backup of a tiny DB roundtrips: same rows, integrity_check=ok."""
    from eta_engine.scripts import hermes_memory_backup

    src = tmp_path / "hermes_memory_store.db"
    dest_dir = tmp_path / "backups"
    _seed_db(src, rows=5)

    result = hermes_memory_backup.run(src=src, dest_dir=dest_dir, keep_last=10)

    assert result["status"] == "ok"
    backup_path = Path(str(result["backup_path"]))
    assert backup_path.exists()
    assert backup_path.parent == dest_dir

    # Open the backup and verify row count + integrity
    conn = sqlite3.connect(str(backup_path))
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert cnt == 5
    assert integrity == "ok"


def test_backup_skips_when_source_missing(tmp_path: Path) -> None:
    """No source DB → status='skipped', no exception, no dest dir created
    unnecessarily."""
    from eta_engine.scripts import hermes_memory_backup

    src = tmp_path / "does_not_exist.db"
    dest_dir = tmp_path / "backups"

    result = hermes_memory_backup.run(src=src, dest_dir=dest_dir)
    assert result["status"] == "skipped"
    assert result["backup_path"] is None


def test_backup_prunes_to_keep_last(tmp_path: Path) -> None:
    """keep_last=2 → only newest 2 backups remain after run."""
    from eta_engine.scripts import hermes_memory_backup

    src = tmp_path / "memory.db"
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    _seed_db(src, rows=2)

    # Manually seed 5 fake old backups with staggered mtimes
    import os
    import time

    for i in range(5):
        p = dest_dir / f"hermes_memory_2025010{i + 1}T000000Z.db"
        p.write_bytes(b"fake")
        # Make them progressively older — i=4 is newest among fakes
        os.utime(p, (time.time() - (10 - i) * 100, time.time() - (10 - i) * 100))

    result = hermes_memory_backup.run(src=src, dest_dir=dest_dir, keep_last=2)
    assert result["status"] == "ok"

    # After: only 2 newest backups remain. The newest is the one we just
    # created; the next newest is the most recent fake.
    remaining = sorted(dest_dir.glob("hermes_memory_*.db"))
    assert len(remaining) == 2


def test_backup_unlinks_corrupt_dest_on_verify_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """If integrity_check fails on the new backup, the corrupt file is removed."""
    from eta_engine.scripts import hermes_memory_backup

    src = tmp_path / "memory.db"
    dest_dir = tmp_path / "backups"
    _seed_db(src)

    # Force verify to always fail
    monkeypatch.setattr(
        hermes_memory_backup,
        "verify_backup",
        lambda p: False,
    )

    result = hermes_memory_backup.run(src=src, dest_dir=dest_dir, keep_last=10)
    assert result["status"] == "verify_failed"
    # Backup file should have been cleaned up
    assert list(dest_dir.glob("hermes_memory_*.db")) == []


def test_verify_backup_detects_corrupt_db(tmp_path: Path) -> None:
    """A non-SQLite file → verify_backup returns False, no exception."""
    from eta_engine.scripts import hermes_memory_backup

    bad = tmp_path / "garbage.db"
    bad.write_bytes(b"this is not a sqlite file at all" * 100)

    assert hermes_memory_backup.verify_backup(bad) is False


def test_backup_module_has_main_entry_point() -> None:
    """The script must be runnable as ``python -m`` for the scheduled task."""
    from eta_engine.scripts import hermes_memory_backup

    assert callable(hermes_memory_backup.main)
    assert callable(hermes_memory_backup.run)
    assert callable(hermes_memory_backup.backup_db)
    assert callable(hermes_memory_backup.prune_old_backups)
    assert callable(hermes_memory_backup.verify_backup)
