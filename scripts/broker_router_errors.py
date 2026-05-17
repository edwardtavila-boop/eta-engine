from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

from eta_engine.obs.decision_journal import Actor, Outcome

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping
    from pathlib import Path


class _OrderLike(Protocol):
    bot_id: str
    signal_id: str

    def to_dict(self) -> dict[str, object]: ...


class BrokerRouterErrorHandlers:
    """Own broker-router failure classification side effects."""

    def __init__(
        self,
        *,
        counts: MutableMapping[str, int],
        dry_run: bool,
        quarantine_dir: Path,
        failed_dir: Path,
        atomic_move: Callable[[Path, Path], None],
        clear_retry_meta: Callable[[Path], None],
        record_event: Callable[[str, str, str], None],
        safe_journal: Callable[..., None],
    ) -> None:
        self._counts = counts
        self._dry_run = dry_run
        self._quarantine_dir = quarantine_dir
        self._failed_dir = failed_dir
        self._atomic_move = atomic_move
        self._clear_retry_meta = clear_retry_meta
        self._record_event = record_event
        self._safe_journal = safe_journal

    def handle_routing_config_unsupported(
        self,
        order: _OrderLike,
        target: Path,
        reason: str,
    ) -> None:
        """Quarantine an unmappable (bot, symbol, venue) triple. NOTED journal."""
        self._counts["quarantined"] += 1
        self._record_event(
            target.name,
            "quarantined",
            "routing_config_unsupported_pair",
        )
        if not self._dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self._quarantine_dir / target.name)
            self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_quarantined",
            rationale=f"routing_config_unsupported_pair: {reason}",
            outcome=Outcome.NOTED,
            links=[
                f"signal:{order.signal_id}",
                f"bot:{order.bot_id}",
                f"file:{target.name}",
            ],
            metadata={
                "reason": "routing_config_unsupported_pair",
                "detail": reason,
                "order": order.to_dict(),
            },
        )

    def handle_dormant_broker(
        self,
        order: _OrderLike,
        target: Path,
        venue_name: str,
    ) -> None:
        """Fail closed when routing config points at a dormant broker."""
        reason = (
            f"broker_dormancy: venue={venue_name!r} is dormant; set "
            "ETA_TRADOVATE_ENABLED=1 only for approved prop-fund testing"
        )
        self._counts["failed"] += 1
        self._record_event(target.name, "broker_dormant", reason)
        if not self._dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self._failed_dir / target.name)
            self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_broker_dormant",
            rationale=reason,
            outcome=Outcome.FAILED,
            links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}", f"venue:{venue_name}"],
            metadata={"reason": "broker_dormant", "detail": reason, "order": order.to_dict()},
        )

    def handle_processing_error(self, target: Path, reason: str) -> None:
        """Fail one inconsistent work item without killing the router loop."""
        self._counts["failed"] += 1
        self._record_event(target.name, "processing_error", reason)
        if not self._dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self._failed_dir / target.name)
            self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_processing_error",
            rationale=reason,
            outcome=Outcome.FAILED,
            links=[f"file:{target.name}"],
            metadata={"reason": reason, "path": str(target)},
        )

    def handle_routing_error(
        self,
        order: _OrderLike,
        target: Path,
        reason: str,
    ) -> None:
        """Move to failed/, journal, increment counters."""
        self._counts["failed"] += 1
        self._record_event(target.name, "routing_error", reason)
        if not self._dry_run:
            with contextlib.suppress(OSError):
                self._atomic_move(target, self._failed_dir / target.name)
            self._clear_retry_meta(target)
        self._safe_journal(
            actor=Actor.STRATEGY_ROUTER,
            intent="pending_order_routing_error",
            rationale=reason,
            outcome=Outcome.FAILED,
            links=[f"signal:{order.signal_id}", f"bot:{order.bot_id}"],
            metadata={"reason": reason, "order": order.to_dict()},
        )
