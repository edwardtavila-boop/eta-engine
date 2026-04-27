"""
EVOLUTIONARY TRADING ALGO  //  scripts.live_supervisor
==========================================
Live runtime supervisor — JARVIS-aware order router.

Why this exists
---------------
The supervised live runtime needs to deduplicate order submissions on
reconnect. If the runtime crashes mid-order or the network drops between
local-side place + venue-side ack, a naive retry would submit the same
order twice. The router below stamps every outbound ``OrderRequest`` with
a deterministic ``client_order_id`` derived from the request payload --
identical requests get identical IDs, so the venue's existing duplicate-
order rejection (every supported venue has one) is the dedup mechanism.

The full live-supervisor wraps this router with JARVIS-aware verdict
routing, alert dispatch, kill-switch latching, and reconciliation. This
module ships the ``_ensure_client_order_id`` primitive that the
preflight ``idempotent_order_id`` gate validates; the higher-level
JARVIS integration lives in ``scripts.run_eta_live`` and
``scripts.mnq_live_supervisor``.

Why a deterministic hash, not a UUID?
  * UUIDs would generate a fresh ID on every retry -- defeating the
    point of dedup.
  * Hashing the request payload (symbol + side + qty + order_type +
    price + reduce_only) means the same intent always produces the
    same ID. Reconnect replay = same ID = venue rejects duplicate.

Hash field selection
  * ``client_order_id`` itself is excluded from the input (so passing
    a request through twice is idempotent on the second pass).
  * The 8-decimal float formats match ``venues.base.VenueBase.idempotency_key``
    so the COID this router emits matches what the venue would have
    computed on its own — there's a single canonical hash form.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.venues.base import OrderRequest


class JarvisAwareRouter:
    """JARVIS-aware order router with deterministic client-order-id dedup.

    The full router (failover policy, urgency-aware venue selection,
    JARVIS verdict integration) lives in :class:`eta_engine.venues.router.SmartRouter`
    and the live-runtime wiring in :mod:`eta_engine.scripts.run_eta_live`.
    This class exposes the dedup primitive used by the preflight
    ``idempotent_order_id`` gate.
    """

    #: Truncation length for the hex client-order-id. 32 hex chars =
    #: 128 bits = collision-resistant in practice for any single
    #: account's order stream while staying short enough to fit in
    #: every supported venue's COID field (Tradovate cOID 64ch [DORMANT
    #: per dormancy_mandate / Appendix A], Bybit orderLinkId 36ch, IBKR
    #: cOID 64ch, OKX clOrdId 32ch — 32 fits the tightest constraint).
    COID_LEN: int = 32

    @staticmethod
    def _ensure_client_order_id(request: OrderRequest) -> OrderRequest:
        """Return ``request`` with ``client_order_id`` populated.

        If the input already has ``client_order_id`` set, returns the
        request unchanged. Otherwise, computes a deterministic
        SHA-256-derived id from the request payload (same fields as
        :meth:`eta_engine.venues.base.VenueBase.idempotency_key`)
        and returns a copy with ``client_order_id`` set to the hex
        digest truncated to :attr:`COID_LEN`.

        The result is **idempotent** by contract: two ``OrderRequest``
        instances with identical payloads produce identical
        ``client_order_id`` values. This is the property the preflight
        ``idempotent_order_id`` gate validates.
        """
        if request.client_order_id:
            return request
        payload = "|".join(
            [
                request.symbol,
                request.side.value,
                f"{request.qty:.8f}",
                request.order_type.value,
                f"{request.price or 0.0:.8f}",
                str(request.reduce_only),
            ]
        )
        coid = hashlib.sha256(payload.encode()).hexdigest()[: JarvisAwareRouter.COID_LEN]
        return request.model_copy(update={"client_order_id": coid})


__all__ = ["JarvisAwareRouter"]
