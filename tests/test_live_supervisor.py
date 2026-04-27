"""Tests for :mod:`eta_engine.scripts.live_supervisor`.

Pins the deterministic-COID dedup contract that the preflight
``idempotent_order_id`` gate validates: identical ``OrderRequest``
payloads MUST produce identical ``client_order_id`` values, so a
post-reconnect retry resubmits the same id and the venue's existing
duplicate-rejection mechanism kicks in.
"""

from __future__ import annotations

from eta_engine.scripts.live_supervisor import JarvisAwareRouter
from eta_engine.venues.base import OrderRequest, OrderType, Side


def _req(
    *,
    symbol: str = "MNQZ5",
    side: Side = Side.BUY,
    qty: float = 1.0,
    order_type: OrderType = OrderType.MARKET,
    price: float | None = None,
    reduce_only: bool = False,
    client_order_id: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        price=price,
        reduce_only=reduce_only,
        client_order_id=client_order_id,
    )


# ---------------------------------------------------------------------------
# Idempotency -- the core dedup contract
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Identical payloads MUST produce identical client_order_ids."""

    def test_two_identical_requests_get_same_coid(self) -> None:
        a = JarvisAwareRouter._ensure_client_order_id(_req())
        b = JarvisAwareRouter._ensure_client_order_id(_req())
        assert a.client_order_id is not None
        assert a.client_order_id == b.client_order_id

    def test_coid_is_32_hex_chars(self) -> None:
        a = JarvisAwareRouter._ensure_client_order_id(_req())
        assert a.client_order_id is not None
        assert len(a.client_order_id) == 32
        # Hex-only: digits + a-f
        assert all(c in "0123456789abcdef" for c in a.client_order_id)

    def test_coid_is_deterministic_across_runs(self) -> None:
        """The same request payload must always hash to the same coid
        -- this is what lets reconnect-replay logic dedupe at the venue."""
        coids = {JarvisAwareRouter._ensure_client_order_id(_req()).client_order_id for _ in range(10)}
        assert len(coids) == 1


# ---------------------------------------------------------------------------
# Preserves a caller-supplied COID
# ---------------------------------------------------------------------------


class TestPreservesProvidedCoid:
    """If the caller already set client_order_id, never overwrite it."""

    def test_existing_coid_is_preserved(self) -> None:
        req = _req(client_order_id="caller-supplied-id")
        out = JarvisAwareRouter._ensure_client_order_id(req)
        assert out.client_order_id == "caller-supplied-id"

    def test_existing_coid_returned_unchanged_request(self) -> None:
        """Passing a request with COID already set should be a no-op."""
        req = _req(client_order_id="abc123")
        out = JarvisAwareRouter._ensure_client_order_id(req)
        # The request is returned (may or may not be the same instance,
        # but content matches).
        assert out.client_order_id == req.client_order_id
        assert out.symbol == req.symbol
        assert out.qty == req.qty


# ---------------------------------------------------------------------------
# Different inputs -> different COIDs
# ---------------------------------------------------------------------------


class TestDistinctness:
    """Different payloads MUST produce different coids -- the hash must
    actually depend on every payload field, otherwise two genuinely
    different orders would collide and one would be silently dropped."""

    def test_different_symbol_different_coid(self) -> None:
        a = JarvisAwareRouter._ensure_client_order_id(_req(symbol="MNQZ5"))
        b = JarvisAwareRouter._ensure_client_order_id(_req(symbol="NQZ5"))
        assert a.client_order_id != b.client_order_id

    def test_different_side_different_coid(self) -> None:
        a = JarvisAwareRouter._ensure_client_order_id(_req(side=Side.BUY))
        b = JarvisAwareRouter._ensure_client_order_id(_req(side=Side.SELL))
        assert a.client_order_id != b.client_order_id

    def test_different_qty_different_coid(self) -> None:
        a = JarvisAwareRouter._ensure_client_order_id(_req(qty=1.0))
        b = JarvisAwareRouter._ensure_client_order_id(_req(qty=2.0))
        assert a.client_order_id != b.client_order_id

    def test_different_order_type_different_coid(self) -> None:
        a = JarvisAwareRouter._ensure_client_order_id(
            _req(order_type=OrderType.MARKET),
        )
        b = JarvisAwareRouter._ensure_client_order_id(
            _req(order_type=OrderType.LIMIT, price=20000.0),
        )
        assert a.client_order_id != b.client_order_id

    def test_different_price_different_coid(self) -> None:
        a = JarvisAwareRouter._ensure_client_order_id(
            _req(order_type=OrderType.LIMIT, price=20000.0),
        )
        b = JarvisAwareRouter._ensure_client_order_id(
            _req(order_type=OrderType.LIMIT, price=20001.0),
        )
        assert a.client_order_id != b.client_order_id

    def test_different_reduce_only_different_coid(self) -> None:
        a = JarvisAwareRouter._ensure_client_order_id(_req(reduce_only=False))
        b = JarvisAwareRouter._ensure_client_order_id(_req(reduce_only=True))
        assert a.client_order_id != b.client_order_id


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_market_order_with_no_price_hashes_cleanly(self) -> None:
        """Market orders pass price=None; the helper must handle that
        without crashing or producing a nonsensical hash."""
        req = _req(price=None, order_type=OrderType.MARKET)
        out = JarvisAwareRouter._ensure_client_order_id(req)
        assert out.client_order_id is not None
        assert len(out.client_order_id) == 32

    def test_zero_price_and_none_price_hash_identically(self) -> None:
        """The canonical hash form treats price=None as 0.0 (matching
        venues.base.VenueBase.idempotency_key). This is the contract;
        if it changed, callers would suddenly produce different coids
        for the same intent."""
        a = JarvisAwareRouter._ensure_client_order_id(_req(price=None))
        b = JarvisAwareRouter._ensure_client_order_id(
            _req(order_type=OrderType.MARKET, price=0.0),
        )
        assert a.client_order_id == b.client_order_id

    def test_classmethod_callable_without_instance(self) -> None:
        """The preflight gate calls JarvisAwareRouter._ensure_client_order_id
        without instantiating; the contract must support that."""
        # No `JarvisAwareRouter()` -- direct staticmethod call.
        out = JarvisAwareRouter._ensure_client_order_id(_req())
        assert out.client_order_id is not None
