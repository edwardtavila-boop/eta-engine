"""EVOLUTIONARY TRADING ALGO // safety.idempotency.

Order-submission dedup log. Each outbound order carries a
deterministic ``client_order_id`` derived from the request payload
(see ``scripts.live_supervisor.JarvisAwareRouter._ensure_client_order_id``).
A retry of the same intent therefore arrives with the same id; this
module keeps a process-local cache so the second submission never
re-routes to the venue.

Design notes
------------
* The intended production backend is the Supabase ``order_intents``
  table -- a single row per ``client_order_id`` carries the
  authoritative state. The functions below speak that contract
  (``status``, ``broker_order_id``, ``response_payload``) so the
  in-memory backing store here is a drop-in for tests / dev / paper
  while the Supabase wiring matures.
* :class:`IdempotencyError` is a typed marker so the venue can
  fail-closed without conflating "store is unreachable" with
  ordinary "duplicate id".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


class IdempotencyError(RuntimeError):
    """Raised when the dedup store cannot service a request.

    The exception's ``.reason`` attribute carries a short stable code
    (``"store_unreachable"`` / ``"corrupt_record"`` / ...).
    """

    def __init__(self, message: str, *, reason: str = "store_unreachable") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Immutable view of one row in the dedup log.

    ``is_new`` is ``True`` only for the FIRST call to
    :func:`check_or_register` with a given ``client_order_id``.
    Every subsequent call returns the cached record verbatim, so
    a retry skips re-routing and surfaces the cached venue
    response back to the caller.
    """

    client_order_id: str
    venue: str
    symbol: str
    is_new: bool
    status: str = "pending"
    broker_order_id: str | None = None
    note: str = ""
    response_payload: dict[str, Any] | None = None
    intent_payload: dict[str, Any] = field(default_factory=dict)


_LOCK: threading.Lock = threading.Lock()
_STORE: dict[str, IdempotencyRecord] = {}


def check_or_register(
    *,
    client_order_id: str,
    venue: str,
    symbol: str,
    intent_payload: dict[str, Any],
) -> IdempotencyRecord:
    """Return the existing record for ``client_order_id``, or
    register a fresh one and return it with ``is_new=True``.

    First-call path: inserts a new ``pending`` row and returns
    ``is_new=True`` so the venue should route the order.
    Repeat-call path: returns the cached record with
    ``is_new=False`` so the venue should NOT re-route.
    """
    if not client_order_id:
        raise IdempotencyError(
            "client_order_id must be non-empty", reason="invalid_id"
        )
    with _LOCK:
        existing = _STORE.get(client_order_id)
        if existing is not None:
            return IdempotencyRecord(
                client_order_id=existing.client_order_id,
                venue=existing.venue,
                symbol=existing.symbol,
                is_new=False,
                status=existing.status,
                broker_order_id=existing.broker_order_id,
                note=existing.note or "duplicate",
                response_payload=existing.response_payload,
                intent_payload=existing.intent_payload,
            )
        fresh = IdempotencyRecord(
            client_order_id=client_order_id,
            venue=venue,
            symbol=symbol,
            is_new=True,
            status="pending",
            broker_order_id=None,
            note="",
            response_payload=None,
            intent_payload=dict(intent_payload),
        )
        _STORE[client_order_id] = fresh
        return fresh


def record_result(
    *,
    client_order_id: str,
    status: str,
    broker_order_id: str | None = None,
    response_payload: dict[str, Any] | None = None,
) -> IdempotencyRecord:
    """Update the dedup log with the venue's final verdict.

    Called after :func:`check_or_register` returned ``is_new=True``
    and the order has been routed. Idempotent: writing the same
    terminal status twice is a no-op.
    """
    if not client_order_id:
        raise IdempotencyError(
            "client_order_id must be non-empty", reason="invalid_id"
        )
    with _LOCK:
        existing = _STORE.get(client_order_id)
        if existing is None:
            raise IdempotencyError(
                f"no pending record for {client_order_id!r}",
                reason="missing_record",
            )
        updated = IdempotencyRecord(
            client_order_id=existing.client_order_id,
            venue=existing.venue,
            symbol=existing.symbol,
            is_new=False,
            status=status,
            broker_order_id=broker_order_id or existing.broker_order_id,
            note=existing.note,
            response_payload=response_payload or existing.response_payload,
            intent_payload=existing.intent_payload,
        )
        _STORE[client_order_id] = updated
        return updated


def reset_store_for_test() -> None:
    """Test hook: drop every record from the in-memory store."""
    with _LOCK:
        _STORE.clear()


__all__ = [
    "IdempotencyError",
    "IdempotencyRecord",
    "check_or_register",
    "record_result",
    "reset_store_for_test",
]
