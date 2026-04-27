"""Unit tests for :mod:`eta_engine.core.runtime_log_rotator`.

M3 closure (v0.1.67) -- pins the rotation / gzip / prune semantics so
a regression in any of the three legs surfaces immediately. The
rotator is the disk-quota safety net for the runtime log; if it
silently breaks, the eval VM fills its disk over the course of a
month.

Sections
--------
TestConstructorValidation
  Bounds checks: positive size threshold, non-negative ages,
  retain >= gzip-after.

TestSizeTriggeredRotation
  Live log under threshold -> no-op.
  Live log over threshold -> renamed to a timestamped sibling,
  fresh log_path is gone (caller will re-create on next write).

TestGzipAged
  Recent rotated archives stay uncompressed.
  Old rotated archives become *.jsonl.gz, original removed.

TestPruneAged
  Recent gzipped archives are kept.
  Aged gzipped archives are deleted.

TestRunComposition
  ``run()`` chains rotate -> gzip -> prune in one call and surfaces
  the outcome dict.

TestStats
  Rotation / gzip / prune counters increment correctly across calls.

TestEdgeCases
  Missing live log -> rotation no-op.
  Missing parent dir -> gzip / prune no-op.
  Same-second rotation collision -> archive name gets a counter suffix.
"""

from __future__ import annotations

import gzip
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from eta_engine.core.runtime_log_rotator import (
    DEFAULT_GZIP_AFTER_AGE_SECONDS,
    DEFAULT_RETAIN_AGE_SECONDS,
    DEFAULT_ROTATE_AT_SIZE_BYTES,
    RuntimeLogRotator,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_bytes(path: Path, n: int) -> None:
    """Create / overwrite ``path`` with ``n`` bytes of dummy content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * n)


def _set_mtime(path: Path, age_seconds: float) -> None:
    """Backdate ``path``'s mtime to ``now - age_seconds``."""
    target = datetime.now(UTC).timestamp() - age_seconds
    os.utime(path, (target, target))


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_default_values_are_sensible(self, tmp_path: Path) -> None:
        rotator = RuntimeLogRotator(log_path=tmp_path / "rt.jsonl")
        assert rotator.rotate_at_size_bytes == DEFAULT_ROTATE_AT_SIZE_BYTES
        assert rotator.gzip_after_age_seconds == DEFAULT_GZIP_AFTER_AGE_SECONDS
        assert rotator.retain_age_seconds == DEFAULT_RETAIN_AGE_SECONDS

    def test_zero_size_threshold_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="rotate_at_size_bytes"):
            RuntimeLogRotator(
                log_path=tmp_path / "rt.jsonl",
                rotate_at_size_bytes=0,
            )

    def test_negative_size_threshold_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="rotate_at_size_bytes"):
            RuntimeLogRotator(
                log_path=tmp_path / "rt.jsonl",
                rotate_at_size_bytes=-1,
            )

    def test_negative_gzip_age_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="gzip_after_age_seconds"):
            RuntimeLogRotator(
                log_path=tmp_path / "rt.jsonl",
                gzip_after_age_seconds=-1,
            )

    def test_negative_retain_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="retain_age_seconds"):
            RuntimeLogRotator(
                log_path=tmp_path / "rt.jsonl",
                retain_age_seconds=-1,
            )

    def test_retain_below_gzip_age_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="retain_age_seconds"):
            RuntimeLogRotator(
                log_path=tmp_path / "rt.jsonl",
                gzip_after_age_seconds=86_400.0,
                retain_age_seconds=3_600.0,
            )


# ---------------------------------------------------------------------------
# Size-triggered rotation
# ---------------------------------------------------------------------------


class TestSizeTriggeredRotation:
    def test_under_threshold_is_noop(self, tmp_path: Path) -> None:
        log = tmp_path / "rt.jsonl"
        _write_bytes(log, 100)
        rotator = RuntimeLogRotator(
            log_path=log,
            rotate_at_size_bytes=10_000,
        )
        result = rotator.maybe_rotate(datetime.now(UTC))
        assert result is None
        assert log.exists()
        assert rotator.stats.rotations == 0

    def test_over_threshold_renames_to_timestamped_archive(
        self,
        tmp_path: Path,
    ) -> None:
        log = tmp_path / "rt.jsonl"
        _write_bytes(log, 5_000)
        rotator = RuntimeLogRotator(
            log_path=log,
            rotate_at_size_bytes=1_000,
        )
        now = datetime(2026, 4, 25, 17, 30, 45, tzinfo=UTC)
        archive = rotator.maybe_rotate(now)
        assert archive is not None
        assert archive.name == "rt.2026-04-25T17-30-45Z.jsonl"
        assert archive.exists()
        # The live log is gone -- caller will re-create it on next write.
        assert not log.exists()
        assert rotator.stats.rotations == 1

    def test_missing_live_log_is_noop(self, tmp_path: Path) -> None:
        rotator = RuntimeLogRotator(
            log_path=tmp_path / "never_existed.jsonl",
            rotate_at_size_bytes=10,
        )
        assert rotator.maybe_rotate(datetime.now(UTC)) is None
        assert rotator.stats.rotations == 0

    def test_same_second_collision_appends_counter(
        self,
        tmp_path: Path,
    ) -> None:
        log = tmp_path / "rt.jsonl"
        _write_bytes(log, 5_000)
        rotator = RuntimeLogRotator(
            log_path=log,
            rotate_at_size_bytes=1_000,
        )
        now = datetime(2026, 4, 25, 17, 30, 45, tzinfo=UTC)
        first = rotator.maybe_rotate(now)
        assert first is not None
        # Re-create the live log and rotate again at the same second.
        _write_bytes(log, 5_000)
        second = rotator.maybe_rotate(now)
        assert second is not None
        assert second != first
        assert second.name == "rt.2026-04-25T17-30-45Z.1.jsonl"


# ---------------------------------------------------------------------------
# Gzip aging
# ---------------------------------------------------------------------------


class TestGzipAged:
    def test_recent_archive_not_gzipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "rt.2026-04-25T15-00-00Z.jsonl"
        _write_bytes(archive, 1_000)
        # Mtime fresh -- well below the 24h gzip threshold.
        _set_mtime(archive, age_seconds=60.0)
        rotator = RuntimeLogRotator(
            log_path=tmp_path / "rt.jsonl",
            gzip_after_age_seconds=3_600.0,
            retain_age_seconds=7_200.0,
        )
        result = rotator.gzip_aged(datetime.now(UTC))
        assert result == []
        assert archive.exists()

    def test_aged_archive_gets_gzipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "rt.2026-04-25T01-00-00Z.jsonl"
        archive.write_bytes(b"line1\nline2\n")
        _set_mtime(archive, age_seconds=2 * 3_600)  # 2h old
        rotator = RuntimeLogRotator(
            log_path=tmp_path / "rt.jsonl",
            gzip_after_age_seconds=3_600.0,  # 1h
            retain_age_seconds=86_400.0,
        )
        result = rotator.gzip_aged(datetime.now(UTC))
        assert len(result) == 1
        gz = result[0]
        assert gz.suffix == ".gz"
        assert gz.exists()
        assert not archive.exists()
        # Round-trip the content to confirm the gzip is valid.
        with gzip.open(gz, "rb") as fh:
            assert fh.read() == b"line1\nline2\n"
        assert rotator.stats.gzipped == 1

    def test_existing_gz_with_stale_uncompressed_cleans_up(
        self,
        tmp_path: Path,
    ) -> None:
        archive = tmp_path / "rt.2026-04-25T01-00-00Z.jsonl"
        archive.write_bytes(b"stale leftover")
        _set_mtime(archive, age_seconds=2 * 3_600)
        gz = archive.with_suffix(archive.suffix + ".gz")
        gz.write_bytes(b"prior gzip output")
        rotator = RuntimeLogRotator(
            log_path=tmp_path / "rt.jsonl",
            gzip_after_age_seconds=3_600.0,
            retain_age_seconds=86_400.0,
        )
        rotator.gzip_aged(datetime.now(UTC))
        # The stale uncompressed leftover is removed; the existing
        # gzip is preserved (we don't overwrite a possibly-good gzip
        # with a possibly-stale .jsonl).
        assert not archive.exists()
        assert gz.exists()


# ---------------------------------------------------------------------------
# Retention prune
# ---------------------------------------------------------------------------


class TestPruneAged:
    def test_recent_gzipped_archive_kept(self, tmp_path: Path) -> None:
        gz = tmp_path / "rt.2026-04-25T01-00-00Z.jsonl.gz"
        gz.write_bytes(b"")
        _set_mtime(gz, age_seconds=3_600)  # 1h old
        rotator = RuntimeLogRotator(
            log_path=tmp_path / "rt.jsonl",
            gzip_after_age_seconds=60.0,
            retain_age_seconds=86_400.0,  # 24h
        )
        result = rotator.prune_aged(datetime.now(UTC))
        assert result == []
        assert gz.exists()

    def test_aged_gzipped_archive_deleted(self, tmp_path: Path) -> None:
        gz = tmp_path / "rt.2026-04-01T01-00-00Z.jsonl.gz"
        gz.write_bytes(b"")
        _set_mtime(gz, age_seconds=10 * 86_400)  # 10 days old
        rotator = RuntimeLogRotator(
            log_path=tmp_path / "rt.jsonl",
            gzip_after_age_seconds=60.0,
            retain_age_seconds=7 * 86_400.0,  # 7 days
        )
        result = rotator.prune_aged(datetime.now(UTC))
        assert result == [gz]
        assert not gz.exists()
        assert rotator.stats.pruned == 1


# ---------------------------------------------------------------------------
# run() composition
# ---------------------------------------------------------------------------


class TestRunComposition:
    def test_run_chains_rotate_gzip_prune(self, tmp_path: Path) -> None:
        log = tmp_path / "rt.jsonl"
        _write_bytes(log, 5_000)
        # Pre-existing aged archive that should get gzipped.
        old_archive = tmp_path / "rt.2026-04-24T00-00-00Z.jsonl"
        old_archive.write_bytes(b"yesterday")
        _set_mtime(old_archive, age_seconds=2 * 3_600)
        # Pre-existing very-old gz that should get pruned.
        ancient = tmp_path / "rt.2026-03-01T00-00-00Z.jsonl.gz"
        ancient.write_bytes(b"ancient")
        _set_mtime(ancient, age_seconds=60 * 86_400)
        rotator = RuntimeLogRotator(
            log_path=log,
            rotate_at_size_bytes=1_000,
            gzip_after_age_seconds=3_600.0,
            retain_age_seconds=30 * 86_400.0,
        )
        outcome = rotator.run(datetime.now(UTC))
        assert len(outcome["rotated"]) == 1
        assert len(outcome["gzipped"]) == 1
        assert len(outcome["pruned"]) == 1


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_increment_across_calls(self, tmp_path: Path) -> None:
        log = tmp_path / "rt.jsonl"
        _write_bytes(log, 5_000)
        rotator = RuntimeLogRotator(
            log_path=log,
            rotate_at_size_bytes=1_000,
        )
        rotator.maybe_rotate(datetime.now(UTC))
        _write_bytes(log, 5_000)
        # Two rotations, one second apart.
        rotator.maybe_rotate(datetime.now(UTC) + timedelta(seconds=1))
        assert rotator.stats.rotations == 2

    def test_run_with_no_changes_increments_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        rotator = RuntimeLogRotator(
            log_path=tmp_path / "rt.jsonl",
            rotate_at_size_bytes=1_000,
        )
        outcome = rotator.run()
        assert outcome == {"rotated": [], "gzipped": [], "pruned": []}
        assert rotator.stats.rotations == 0
        assert rotator.stats.gzipped == 0
        assert rotator.stats.pruned == 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_missing_parent_dir_does_not_crash(self, tmp_path: Path) -> None:
        rotator = RuntimeLogRotator(
            log_path=tmp_path / "nope" / "rt.jsonl",
        )
        # All three legs are no-ops on a missing directory.
        outcome = rotator.run()
        assert outcome == {"rotated": [], "gzipped": [], "pruned": []}

    def test_other_files_in_dir_are_ignored(self, tmp_path: Path) -> None:
        # Files that don't match the archive pattern stay untouched
        # even when the rotator processes the directory.
        log = tmp_path / "rt.jsonl"
        _write_bytes(log, 5_000)
        unrelated = tmp_path / "alerts_log.jsonl"
        _write_bytes(unrelated, 1_000)
        _set_mtime(unrelated, age_seconds=365 * 86_400)
        rotator = RuntimeLogRotator(
            log_path=log,
            rotate_at_size_bytes=1_000,
            gzip_after_age_seconds=3_600.0,
            retain_age_seconds=30 * 86_400.0,
        )
        rotator.run()
        # Unrelated file untouched (different stem, not in glob pattern).
        assert unrelated.exists()
