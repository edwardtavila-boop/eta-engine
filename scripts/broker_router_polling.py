from __future__ import annotations

import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping
    from logging import Logger

    from eta_engine.scripts.runtime_order_hold import OrderEntryHold


class _OrderLike(Protocol):
    bot_id: str
    symbol: str


class BrokerRouterPolling:
    """Own poll-loop scanning plus scoped order-entry hold evaluation."""

    def __init__(
        self,
        *,
        pending_dir: Path,
        processing_dir: Path,
        dry_run: bool,
        counts: MutableMapping[str, int],
        order_entry_hold: Callable[[], OrderEntryHold],
        emit_heartbeat: Callable[..., None],
        record_event: Callable[[str, str, str], None],
        process_pending_file: Callable[[Path], Awaitable[None]],
        process_retry_file: Callable[[Path], Awaitable[None]],
        parse_pending_file: Callable[[Path], _OrderLike],
        routing_venue_for: Callable[[str, str], str],
        asset_class_for_symbol: Callable[[str], str],
        logger: Logger,
    ) -> None:
        self._pending_dir = Path(pending_dir)
        self._processing_dir = Path(processing_dir)
        self._dry_run = bool(dry_run)
        self._counts = counts
        self._order_entry_hold = order_entry_hold
        self._emit_heartbeat = emit_heartbeat
        self._record_event = record_event
        self._process_pending_file = process_pending_file
        self._process_retry_file = process_retry_file
        self._parse_pending_file = parse_pending_file
        self._routing_venue_for = routing_venue_for
        self._asset_class_for_symbol = asset_class_for_symbol
        self._logger = logger

    async def tick(self, *, stopped: Callable[[], bool]) -> None:
        """Run one pending + retry scan with heartbeat emission."""
        hold = self._order_entry_hold()
        if hold.active and hold.scope == "all":
            self._counts["held"] += 1
            self._record_event("runtime", "order_entry_hold", hold.reason)
            self._logger.warning(
                "broker_router order-entry hold active; skipping poll reason=%s path=%s",
                hold.reason,
                hold.path,
            )
            self._emit_heartbeat(hold=hold)
            return

        self._emit_heartbeat(hold=hold)

        pending_paths = self._scan_paths(self._pending_dir)
        for path in pending_paths:
            if stopped():
                break
            try:
                await self._process_pending_file(path)
            except Exception:  # noqa: BLE001
                self._logger.error(
                    "unhandled exception processing %s:\n%s",
                    path,
                    traceback.format_exc(),
                )

        if not self._dry_run:
            processing_paths = self._scan_paths(self._processing_dir)
            for target in processing_paths:
                if stopped():
                    break
                try:
                    await self._process_retry_file(target)
                except Exception:  # noqa: BLE001
                    self._logger.error(
                        "unhandled exception in retry %s:\n%s",
                        target,
                        traceback.format_exc(),
                    )

        self._emit_heartbeat(hold=hold)

    def hold_blocks_file(self, path: Path) -> bool:
        """Return True when the runtime hold should leave a file unsubmitted."""
        hold = self._order_entry_hold()
        if not hold.active:
            return False
        if hold.scope == "all":
            blocks = True
            venue_name = "*"
            asset_class = "*"
        else:
            try:
                order = self._parse_pending_file(path)
                venue_name = self._routing_venue_for(order.bot_id, order.symbol)
                asset_class = self._asset_class_for_symbol(order.symbol)
            except Exception:  # noqa: BLE001
                return False
            blocks = hold.blocks(venue=venue_name, asset_class=asset_class)
        if not blocks:
            return False
        self._counts["held"] += 1
        self._record_event(path.name, "order_entry_hold", hold.reason)
        self._logger.warning(
            "pending order held in place: file=%s scope=%s reason=%s venue=%s class=%s path=%s",
            path,
            hold.scope,
            hold.reason,
            venue_name,
            asset_class,
            hold.path,
        )
        return True

    def _scan_paths(self, directory: Path) -> list[Path]:
        try:
            return sorted(directory.glob("*.pending_order.json"))
        except OSError as exc:
            label = "pending" if directory == self._pending_dir else "processing"
            self._logger.warning("%s dir scan failed: %s", label, exc)
            return []
