"""
EVOLUTIONARY TRADING ALGO  //  core.runtime_log_rotator
============================================
M3 closure (v0.1.67) -- rotation + age-based gzip + retention pruning
for ``docs/runtime_log.jsonl``.

Why this exists
---------------
At the v0.1.65 default tick cadence (1.0s in dry-run; 1.0s minimum in
live, validated against the kill-switch cushion) and the v0.1.63+ tick
log shape (one JSON line per tick, plus periodic non-tick events --
runtime_start, kill-switch verdicts, broker_equity drift events), the
monthly log volume is roughly:

  86 400 ticks/day  *  ~250-400 bytes/line  *  30 days  ~=  ~1 GB/month

Plus non-tick events and the v0.1.63 broker_equity sub-key roughly
double that to ~2 GB/month. The Red Team review of v0.1.63 R1 (M3)
estimated 8 GB/month for the absolute worst-case fleet (multiple
bots, full payload, high-event days).

Without rotation, the log grows unbounded. The eval-bust failure mode:
the eval VM hits its disk quota, the runtime cannot append to the log,
and depending on filesystem behaviour either crashes or silently loses
the audit trail. Either way an Apex eval that is otherwise on-track
fails for a preventable infrastructure reason.

What this module does
---------------------
* :class:`RuntimeLogRotator` -- pure-stdlib helper that owns three
  responsibilities:

    1. Size-triggered rotation: when ``log_path`` exceeds
       ``rotate_at_size_bytes``, rename it to a timestamped sibling
       (``runtime_log.jsonl.2026-04-25T15-04-32Z.jsonl``) and let the
       caller open a fresh ``log_path`` on the next write.

    2. Age-based gzip: rotated logs older than
       ``gzip_after_age_seconds`` are compressed in-place to
       ``*.jsonl.gz``. Compression is invoked synchronously here --
       on a 100 MB log this is a few hundred ms, well below a tick
       budget when called every 10 minutes at most.

    3. Retention prune: rotated/gzipped logs older than
       ``retain_age_seconds`` are deleted.

* All three operations are idempotent. ``maybe_rotate(now)`` is a
  no-op when the live log is below the rotation threshold;
  ``gzip_aged(now)`` is a no-op when no rotated logs match the age
  criterion; ``prune_aged(now)`` is a no-op when no compressed logs
  are old enough.

* The ``run(now)`` method composes all three -- the runtime calls it
  every N ticks (or on a wall-clock cadence; either is fine because
  every step is idempotent).

What this module does NOT do (deferred / out of scope)
-------------------------------------------------------
* Does NOT own the live log file handle. The runtime keeps writing
  to ``log_path`` directly. After ``maybe_rotate(now)`` runs the
  rotated file is moved aside; the next write to ``log_path`` opens
  a fresh file via the runtime's existing append pattern. There is
  no need for the rotator to coordinate the file handle because the
  runtime opens-write-closes per call.

* Does NOT provide gzip-on-write. The live log stays uncompressed
  while it is being written; only rotated archives are compressed.
  This keeps the hot-write path simple and means an interrupted
  runtime always leaves the live log in a tail-able state.

* Does NOT handle multi-process write contention. The live file is
  assumed to be written by exactly one process. Concurrent writers
  would race on rotation; the runtime invariant has always been
  "one ApexRuntime per log_path" and the Red Team review did not
  flag that as a residual.

* Does NOT scan upward into parent directories. The rotator only
  considers files matching ``<log_path.stem><suffix>.jsonl[.gz]``
  in the same directory.

Usage
-----
    from eta_engine.core.runtime_log_rotator import RuntimeLogRotator

    rotator = RuntimeLogRotator(
        log_path=Path("docs/runtime_log.jsonl"),
        rotate_at_size_bytes=100 * 1024 * 1024,   # 100 MB
        gzip_after_age_seconds=24 * 3600,         # 1 day
        retain_age_seconds=30 * 24 * 3600,        # 30 days
    )
    # Every Nth tick:
    rotator.run(now=datetime.now(UTC))
"""

from __future__ import annotations

import contextlib
import gzip
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: Default rotation threshold (100 MB). Chosen so a single rotated
#: archive is small enough to ``less`` / ``grep`` without thrashing,
#: but large enough that rotation is rare (a healthy 1s-cadence log
#: hits 100 MB in roughly 4 days).
DEFAULT_ROTATE_AT_SIZE_BYTES: int = 100 * 1024 * 1024

#: Default gzip-after-age (24 hours). Compresses yesterday's rotated
#: archives so the working directory stays readable for the active
#: trading day.
DEFAULT_GZIP_AFTER_AGE_SECONDS: float = 24 * 3600.0

#: Default retention (30 days). Compressed archives older than this
#: are deleted. Picked to align with the v0.1.59 R3 trailing-DD
#: audit-log retention so post-mortem windows are uniform across the
#: codebase.
DEFAULT_RETAIN_AGE_SECONDS: float = 30 * 24 * 3600.0


@dataclass
class RotatorStats:
    """Lifetime counters surfaced via :attr:`RuntimeLogRotator.stats`."""

    rotations: int = 0
    gzipped: int = 0
    pruned: int = 0
    last_rotate_ts: str | None = None
    last_gzip_ts: str | None = None
    last_prune_ts: str | None = None
    errors: list[str] = field(default_factory=list)


class RuntimeLogRotator:
    """Rotate ``docs/runtime_log.jsonl`` so the disk doesn't fill mid-eval.

    Parameters
    ----------
    log_path:
        Path to the live log file. Must be a file (not a directory).
        Does not need to exist yet; rotation no-ops cleanly when
        absent.
    rotate_at_size_bytes:
        Size threshold for the live log. Above this,
        :meth:`maybe_rotate` renames the file aside.
    gzip_after_age_seconds:
        Rotated archives older than this are gzipped in-place.
    retain_age_seconds:
        Compressed archives older than this are deleted.
    """

    def __init__(
        self,
        log_path: Path,
        *,
        rotate_at_size_bytes: int = DEFAULT_ROTATE_AT_SIZE_BYTES,
        gzip_after_age_seconds: float = DEFAULT_GZIP_AFTER_AGE_SECONDS,
        retain_age_seconds: float = DEFAULT_RETAIN_AGE_SECONDS,
    ) -> None:
        if rotate_at_size_bytes <= 0:
            msg = f"rotate_at_size_bytes must be > 0 (got {rotate_at_size_bytes})"
            raise ValueError(msg)
        if gzip_after_age_seconds < 0:
            msg = f"gzip_after_age_seconds must be >= 0 (got {gzip_after_age_seconds})"
            raise ValueError(msg)
        if retain_age_seconds < 0:
            msg = f"retain_age_seconds must be >= 0 (got {retain_age_seconds})"
            raise ValueError(msg)
        if retain_age_seconds < gzip_after_age_seconds:
            msg = (
                f"retain_age_seconds ({retain_age_seconds}) must be "
                f">= gzip_after_age_seconds ({gzip_after_age_seconds}) "
                f"-- pruning before gzipping would delete the only "
                f"copy of the archive"
            )
            raise ValueError(msg)
        self.log_path = Path(log_path)
        self.rotate_at_size_bytes = int(rotate_at_size_bytes)
        self.gzip_after_age_seconds = float(gzip_after_age_seconds)
        self.retain_age_seconds = float(retain_age_seconds)
        self._stats = RotatorStats()

    @property
    def stats(self) -> RotatorStats:
        return self._stats

    # ------------------------------------------------------------------ #
    # Sibling-file discovery
    # ------------------------------------------------------------------ #

    def _archive_pattern_jsonl(self) -> str:
        """Glob pattern for uncompressed rotated archives."""
        return f"{self.log_path.stem}.*.jsonl"

    def _archive_pattern_gz(self) -> str:
        """Glob pattern for compressed rotated archives."""
        return f"{self.log_path.stem}.*.jsonl.gz"

    def _live_archive_dir(self) -> Path:
        return self.log_path.parent

    def _list_jsonl_archives(self) -> list[Path]:
        d = self._live_archive_dir()
        if not d.exists():
            return []
        # Exclude the live log itself -- it has no timestamp suffix.
        return sorted(p for p in d.glob(self._archive_pattern_jsonl()) if p != self.log_path and p.is_file())

    def _list_gz_archives(self) -> list[Path]:
        d = self._live_archive_dir()
        if not d.exists():
            return []
        return sorted(p for p in d.glob(self._archive_pattern_gz()) if p.is_file())

    # ------------------------------------------------------------------ #
    # Rotation
    # ------------------------------------------------------------------ #

    def maybe_rotate(self, now: datetime) -> Path | None:
        """Rotate the live log if it has grown past the size threshold.

        Returns the path of the rotated archive, or ``None`` if no
        rotation was needed. Idempotent on a missing live log.
        """
        if not self.log_path.exists():
            return None
        try:
            size = self.log_path.stat().st_size
        except OSError as exc:
            self._stats.errors.append(f"stat: {exc!r}")
            return None
        if size < self.rotate_at_size_bytes:
            return None
        # Build the archive path. Use a colon-free, sort-friendly stamp
        # so listings stay chronological without parsing.
        stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
        archive = self.log_path.with_name(
            f"{self.log_path.stem}.{stamp}.jsonl",
        )
        # Clash-resilience: if a same-second archive already exists
        # (rotation called twice in <1s), append a counter.
        if archive.exists():
            n = 1
            while True:
                candidate = self.log_path.with_name(
                    f"{self.log_path.stem}.{stamp}.{n}.jsonl",
                )
                if not candidate.exists():
                    archive = candidate
                    break
                n += 1
        try:
            self.log_path.rename(archive)
        except OSError as exc:
            self._stats.errors.append(f"rename: {exc!r}")
            return None
        self._stats.rotations += 1
        self._stats.last_rotate_ts = now.isoformat()
        log.info(
            "runtime_log_rotator: rotated %s -> %s (%d bytes)",
            self.log_path,
            archive,
            size,
        )
        return archive

    # ------------------------------------------------------------------ #
    # Gzip aging
    # ------------------------------------------------------------------ #

    def gzip_aged(self, now: datetime) -> list[Path]:
        """Gzip rotated archives older than ``gzip_after_age_seconds``.

        Returns the list of newly-gzipped archive paths.
        """
        compressed: list[Path] = []
        cutoff = now.timestamp() - self.gzip_after_age_seconds
        for archive in self._list_jsonl_archives():
            try:
                mtime = archive.stat().st_mtime
            except OSError as exc:
                self._stats.errors.append(f"stat: {exc!r}")
                continue
            if mtime > cutoff:
                continue
            gz_path = archive.with_suffix(archive.suffix + ".gz")
            if gz_path.exists():
                # Idempotency: if a gz with this name exists, treat
                # the uncompressed archive as a stale leftover and
                # delete it. Better than refusing to gzip forever.
                try:
                    archive.unlink()
                except OSError as exc:
                    self._stats.errors.append(f"unlink stale: {exc!r}")
                continue
            try:
                with (
                    archive.open("rb") as src,
                    gzip.open(
                        gz_path,
                        "wb",
                    ) as dst,
                ):
                    shutil.copyfileobj(src, dst)
                archive.unlink()
            except OSError as exc:
                self._stats.errors.append(f"gzip: {exc!r}")
                # Best-effort cleanup of partial gz output
                if gz_path.exists():
                    with contextlib.suppress(OSError):
                        gz_path.unlink()
                continue
            compressed.append(gz_path)
            self._stats.gzipped += 1
            self._stats.last_gzip_ts = now.isoformat()
            log.info(
                "runtime_log_rotator: gzipped %s -> %s",
                archive,
                gz_path,
            )
        return compressed

    # ------------------------------------------------------------------ #
    # Retention prune
    # ------------------------------------------------------------------ #

    def prune_aged(self, now: datetime) -> list[Path]:
        """Delete compressed archives older than ``retain_age_seconds``.

        Returns the list of pruned paths.
        """
        pruned: list[Path] = []
        cutoff = now.timestamp() - self.retain_age_seconds
        for archive in self._list_gz_archives():
            try:
                mtime = archive.stat().st_mtime
            except OSError as exc:
                self._stats.errors.append(f"stat: {exc!r}")
                continue
            if mtime > cutoff:
                continue
            try:
                archive.unlink()
            except OSError as exc:
                self._stats.errors.append(f"unlink: {exc!r}")
                continue
            pruned.append(archive)
            self._stats.pruned += 1
            self._stats.last_prune_ts = now.isoformat()
            log.info("runtime_log_rotator: pruned %s", archive)
        return pruned

    # ------------------------------------------------------------------ #
    # One-shot composition
    # ------------------------------------------------------------------ #

    def run(self, now: datetime | None = None) -> dict[str, list[Path]]:
        """Rotate -> gzip -> prune in that order.

        Returns a dict with keys ``rotated`` / ``gzipped`` / ``pruned``
        listing what changed. Useful for tests and for surfacing
        rotation events into the runtime log itself.
        """
        if now is None:
            now = datetime.now(UTC)
        rotated_path = self.maybe_rotate(now)
        rotated = [rotated_path] if rotated_path is not None else []
        gzipped = self.gzip_aged(now)
        pruned = self.prune_aged(now)
        return {"rotated": rotated, "gzipped": gzipped, "pruned": pruned}


__all__ = [
    "DEFAULT_GZIP_AFTER_AGE_SECONDS",
    "DEFAULT_RETAIN_AGE_SECONDS",
    "DEFAULT_ROTATE_AT_SIZE_BYTES",
    "RotatorStats",
    "RuntimeLogRotator",
]
