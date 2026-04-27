"""EVOLUTIONARY TRADING ALGO  //  core.order_state_reconcile.

Self-healing order-state reconciler for reconnect scenarios.

Why this module exists
----------------------
When a venue connection drops mid-session (websocket timeout, network
blip, process restart), the in-memory order book can drift from the
venue's canonical state. Symptoms:

* Order placed locally but the ACK never arrived.
* Order filled on the venue but we still track it as OPEN.
* Order cancelled on the venue but we still have it in-flight locally.

Today each venue adapter handles reconnect ad-hoc. This module gives us
a single typed reconciler: on reconnect, feed it ``(local_orders,
venue_orders)`` and it emits a set of :class:`ReconcileAction` decisions
plus a sanitized local state.

Design
------
* **Pure state-object.** No I/O, no threads. Caller owns the venue fetch
  and the state mutation; we just compute the diff.
* **Conservative.** Any ambiguity resolves to "treat as cancelled locally"
  to avoid double-fills. Caller can override with a policy flag.
* **Idempotent.** Running :meth:`reconcile` twice produces the same action
  set, so a retry after a partial recovery is safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "LocalOrder",
    "VenueOrder",
    "ReconcileAction",
    "ReconcileActionKind",
    "ReconcileReport",
    "OrderStateReconciler",
]


class ReconcileActionKind(StrEnum):
    MARK_FILLED = "MARK_FILLED"  # venue shows filled; local still open
    MARK_CANCELLED = "MARK_CANCELLED"  # venue no longer tracks; local still open
    MARK_PARTIAL = "MARK_PARTIAL"  # venue partial-filled; local shows open/partial
    ACCEPT_VENUE = "ACCEPT_VENUE"  # venue has order we don't know about
    RESOLVE_MISSING = "RESOLVE_MISSING"  # local in-flight, venue has no record
    NOOP = "NOOP"  # states agree, nothing to do


@dataclass(frozen=True)
class LocalOrder:
    """Snapshot of an order as tracked locally before reconciliation."""

    client_order_id: str
    symbol: str
    status: str  # OPEN / PARTIAL / FILLED / CANCELLED / REJECTED
    qty: float
    filled_qty: float = 0.0
    venue_order_id: str | None = None


@dataclass(frozen=True)
class VenueOrder:
    """Snapshot of an order as reported by the venue at reconnect."""

    venue_order_id: str
    client_order_id: str
    symbol: str
    status: str
    qty: float
    filled_qty: float = 0.0


@dataclass(frozen=True)
class ReconcileAction:
    """One decision emitted by the reconciler."""

    kind: ReconcileActionKind
    client_order_id: str
    symbol: str
    reason: str
    venue_order_id: str | None = None
    expected_local_status: str | None = None
    canonical_status: str | None = None
    canonical_filled_qty: float = 0.0


@dataclass(frozen=True)
class ReconcileReport:
    """Aggregate reconciliation decision bundle."""

    actions: tuple[ReconcileAction, ...]
    reconciled_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))

    def actions_of_kind(self, kind: ReconcileActionKind) -> tuple[ReconcileAction, ...]:
        return tuple(a for a in self.actions if a.kind == kind)

    @property
    def has_divergence(self) -> bool:
        return any(a.kind != ReconcileActionKind.NOOP for a in self.actions)


_TERMINAL_STATES: frozenset[str] = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED"})


class OrderStateReconciler:
    """Compute the reconcile action set from local vs venue order snapshots.

    Usage::

        rec = OrderStateReconciler()
        report = rec.reconcile(local_orders, venue_orders)
        for action in report.actions:
            apply_to_local_state(action)
    """

    def __init__(self, *, conservative: bool = True) -> None:
        self.conservative = bool(conservative)

    def reconcile(
        self,
        local: Mapping[str, LocalOrder],
        venue: Mapping[str, VenueOrder],
    ) -> ReconcileReport:
        """Return the reconciliation decisions.

        Inputs are keyed by ``client_order_id``. Any client_order_id
        present on one side but not the other triggers a corrective
        action. States that agree produce a ``NOOP`` entry so callers
        can audit the full reconciliation run.
        """
        actions: list[ReconcileAction] = []
        known_coids = set(local.keys()) | set(venue.keys())
        for coid in sorted(known_coids):
            local_order = local.get(coid)
            venue_order = venue.get(coid)

            if local_order is None and venue_order is not None:
                # Venue has it, we don't. Accept it as canonical.
                actions.append(
                    ReconcileAction(
                        kind=ReconcileActionKind.ACCEPT_VENUE,
                        client_order_id=coid,
                        symbol=venue_order.symbol,
                        venue_order_id=venue_order.venue_order_id,
                        reason="venue has order not tracked locally",
                        canonical_status=venue_order.status,
                        canonical_filled_qty=venue_order.filled_qty,
                    )
                )
                continue

            if venue_order is None and local_order is not None:
                # Local thinks it's alive, venue doesn't know it.
                if local_order.status in _TERMINAL_STATES:
                    actions.append(
                        ReconcileAction(
                            kind=ReconcileActionKind.NOOP,
                            client_order_id=coid,
                            symbol=local_order.symbol,
                            reason="local in terminal state; venue correctly absent",
                            expected_local_status=local_order.status,
                        )
                    )
                    continue
                kind = ReconcileActionKind.MARK_CANCELLED if self.conservative else ReconcileActionKind.RESOLVE_MISSING
                actions.append(
                    ReconcileAction(
                        kind=kind,
                        client_order_id=coid,
                        symbol=local_order.symbol,
                        reason="local open but venue has no record",
                        expected_local_status=local_order.status,
                        canonical_status="CANCELLED" if self.conservative else "UNKNOWN",
                    )
                )
                continue

            assert local_order is not None and venue_order is not None
            action = self._reconcile_pair(local_order, venue_order)
            actions.append(action)

        return ReconcileReport(actions=tuple(actions))

    def _reconcile_pair(self, local_order: LocalOrder, venue_order: VenueOrder) -> ReconcileAction:
        coid = local_order.client_order_id
        local_status = local_order.status.upper()
        venue_status = venue_order.status.upper()
        symbol = venue_order.symbol
        filled = venue_order.filled_qty

        if local_status == venue_status and local_order.filled_qty == filled:
            return ReconcileAction(
                kind=ReconcileActionKind.NOOP,
                client_order_id=coid,
                symbol=symbol,
                venue_order_id=venue_order.venue_order_id,
                reason="states match",
                expected_local_status=local_status,
                canonical_status=venue_status,
                canonical_filled_qty=filled,
            )

        if venue_status == "FILLED" and local_status in {"OPEN", "PARTIAL"}:
            return ReconcileAction(
                kind=ReconcileActionKind.MARK_FILLED,
                client_order_id=coid,
                symbol=symbol,
                venue_order_id=venue_order.venue_order_id,
                reason="venue filled while local was alive",
                expected_local_status=local_status,
                canonical_status="FILLED",
                canonical_filled_qty=filled,
            )

        if venue_status == "PARTIAL" and filled > local_order.filled_qty:
            return ReconcileAction(
                kind=ReconcileActionKind.MARK_PARTIAL,
                client_order_id=coid,
                symbol=symbol,
                venue_order_id=venue_order.venue_order_id,
                reason="venue reports more fill than local",
                expected_local_status=local_status,
                canonical_status="PARTIAL",
                canonical_filled_qty=filled,
            )

        if venue_status in {"CANCELLED", "REJECTED", "EXPIRED"} and local_status in {
            "OPEN",
            "PARTIAL",
        }:
            return ReconcileAction(
                kind=ReconcileActionKind.MARK_CANCELLED,
                client_order_id=coid,
                symbol=symbol,
                venue_order_id=venue_order.venue_order_id,
                reason=f"venue shows {venue_status.lower()}; local still {local_status.lower()}",
                expected_local_status=local_status,
                canonical_status=venue_status,
                canonical_filled_qty=filled,
            )

        # Catch-all: venue disagrees in some other way; default to venue truth.
        return ReconcileAction(
            kind=ReconcileActionKind.ACCEPT_VENUE,
            client_order_id=coid,
            symbol=symbol,
            venue_order_id=venue_order.venue_order_id,
            reason=f"mismatch local={local_status} venue={venue_status}",
            expected_local_status=local_status,
            canonical_status=venue_status,
            canonical_filled_qty=filled,
        )
