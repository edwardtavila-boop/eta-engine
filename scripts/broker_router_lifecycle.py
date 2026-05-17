from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping
    from logging import Logger


class BrokerRouterLifecycleDriver:
    """Own fresh-file ingress, retry scans, and exponential backoff checks."""

    def __init__(
        self,
        *,
        dry_run: bool,
        processing_dir: Path,
        retry_meta_suffix: str,
        max_retries: int,
        interval_s: float,
        backoff_cap_s: float,
        counts: MutableMapping[str, int],
        empty_retry_meta: Callable[[], dict[str, Any]],
        hold_blocks_file: Callable[[Path], bool],
        atomic_move: Callable[[Path, Path], None],
        load_retry_meta: Callable[[Path], dict[str, Any]],
        move_to_failed_with_meta: Callable[[Path, dict[str, Any]], None],
        record_event: Callable[[str, str, str], None],
        run_lifecycle: Callable[[Path], Awaitable[None]],
        logger: Logger,
    ) -> None:
        self._dry_run = bool(dry_run)
        self._processing_dir = Path(processing_dir)
        self._retry_meta_suffix = retry_meta_suffix
        self._max_retries = int(max_retries)
        self._interval_s = float(interval_s)
        self._backoff_cap_s = float(backoff_cap_s)
        self._counts = counts
        self._empty_retry_meta = empty_retry_meta
        self._hold_blocks_file = hold_blocks_file
        self._atomic_move = atomic_move
        self._load_retry_meta = load_retry_meta
        self._move_to_failed_with_meta = move_to_failed_with_meta
        self._record_event = record_event
        self._run_lifecycle = run_lifecycle
        self._logger = logger

    async def process_pending_file(self, path: Path) -> None:
        """Fresh-file entry: move-to-processing, then run the lifecycle."""
        if self._hold_blocks_file(path):
            return
        if self._dry_run:
            target = path
        else:
            target = self._processing_dir / path.name
            try:
                self._atomic_move(path, target)
            except OSError as exc:
                self._logger.info("skip (move failed, likely raced): %s (%s)", path.name, exc)
                return
        await self._run_lifecycle(target, retry_meta=self._empty_retry_meta())

    async def process_retry_file(self, target: Path) -> None:
        """Re-process a file already in processing/ using retry-meta sidecars."""
        await self.process_retry_file_with_backoff(target, should_backoff=self.should_backoff)

    async def process_retry_file_with_backoff(
        self,
        target: Path,
        *,
        should_backoff: Callable[[dict[str, Any]], bool],
    ) -> None:
        """Re-process a file already in processing/ using an injected backoff check."""
        if target.name.endswith(self._retry_meta_suffix):
            return
        if not target.name.endswith(".pending_order.json"):
            return
        retry_meta = self._load_retry_meta(target)
        attempts = int(retry_meta.get("attempts", 0))
        if attempts >= self._max_retries:
            self._logger.warning("retry file at max_retries: %s", target.name)
            self._counts["failed"] += 1
            self._record_event(target.name, "failed", "max_retries_on_retry_scan")
            self._move_to_failed_with_meta(target, retry_meta)
            return
        if should_backoff(retry_meta):
            return
        if self._hold_blocks_file(target):
            return
        await self._run_lifecycle(target, retry_meta=retry_meta)

    def should_backoff(self, retry_meta: dict[str, Any]) -> bool:
        """Return True until min(cap, interval * 2**attempts) has elapsed."""
        attempts = int(retry_meta.get("attempts", 0) or 0)
        if attempts <= 0:
            return False
        last_ts = retry_meta.get("last_attempt_ts", "")
        if not last_ts:
            return False
        try:
            last_dt = datetime.fromisoformat(last_ts)
        except (TypeError, ValueError):
            return False
        elapsed = (datetime.now(UTC) - last_dt).total_seconds()
        return elapsed < min(self._backoff_cap_s, self._interval_s * (2**attempts))
