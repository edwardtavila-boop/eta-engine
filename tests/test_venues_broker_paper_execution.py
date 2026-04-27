"""Broker-paper execution tests for Tastytrade + IBKR adapters.

Covers the v0.1.58 real-paper wiring:
  * Tastytrade ``place_order`` calls the cert API when creds are present
    and round-trips the server-returned ``id`` + ``status``.
  * Network-error / missing-httpx paths degrade to mock OPEN with a
    note in ``raw``.
  * ``get_order_status`` merges the server payload into the cache.
  * ``reconcile_orders`` batches status refreshes.
  * ``IbkrClientPortalConfig`` returns the baked-in BTCUSD conid
    (764777976) without an env-supplied map, and picks PAXOS as the
    listing exchange for BTC/ETH spot.
"""

from __future__ import annotations

import asyncio
from typing import Any

from eta_engine.venues.base import (
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from eta_engine.venues.ibkr import (
    IbkrClientPortalConfig,
    IbkrClientPortalVenue,
)
from eta_engine.venues.tastytrade import (
    TastytradeConfig,
    TastytradeVenue,
    _map_tasty_status,
)


# --------------------------------------------------------------------------- #
# IBKR baked-in BTCUSD conid + PAXOS exchange
# --------------------------------------------------------------------------- #
class TestIbkrBakedInConids:
    def test_default_btcusd_conid(self) -> None:
        cfg = IbkrClientPortalConfig(account_id="DU1234567")
        assert cfg.conid_for("BTCUSD") == 764777976

    def test_default_ethusd_conid(self) -> None:
        cfg = IbkrClientPortalConfig(account_id="DU1234567")
        assert cfg.conid_for("ETHUSD") == 764777977

    def test_env_conid_overrides_baked_in(self) -> None:
        cfg = IbkrClientPortalConfig(
            account_id="DU1234567",
            symbol_conids={"BTCUSD": 123},
        )
        assert cfg.conid_for("BTCUSD") == 123

    def test_unknown_symbol_returns_none(self) -> None:
        cfg = IbkrClientPortalConfig(account_id="DU1234567")
        assert cfg.conid_for("DOGEUSD") is None

    def test_btcusd_routes_to_paxos(self) -> None:
        cfg = IbkrClientPortalConfig(account_id="DU1234567")
        assert cfg.exchange_for("BTCUSD") == "PAXOS"
        assert cfg.exchange_for("ETHUSD") == "PAXOS"

    def test_futures_fall_through_to_default_exchange(self) -> None:
        cfg = IbkrClientPortalConfig(
            account_id="DU1234567",
            default_exchange="CME",
        )
        assert cfg.exchange_for("MNQ") == "CME"
        assert cfg.exchange_for("NQ") == "CME"

    def test_missing_requirements_no_longer_demands_conid_map(self) -> None:
        """Now that BTCUSD has a baked-in conid, the adapter is ready
        with only an account id."""
        cfg = IbkrClientPortalConfig(account_id="DU1234567")
        missing = cfg.missing_requirements()
        assert missing == [], f"expected no missing requirements, got {missing}"

    def test_order_payload_uses_baked_in_btcusd_conid_and_paxos(self) -> None:
        cfg = IbkrClientPortalConfig(account_id="DU1234567")
        venue = IbkrClientPortalVenue(cfg)
        req = OrderRequest(
            symbol="BTCUSD",
            side=Side.BUY,
            qty=1.0,
            order_type=OrderType.MARKET,
        )
        conid = cfg.conid_for("BTCUSD")
        assert conid == 764777976
        payload = venue.build_order_payload(req, conid=conid)
        assert payload["conid"] == 764777976
        assert payload["listingExchange"] == "PAXOS"
        assert payload["side"] == "BUY"
        assert payload["ticker"] == "BTCUSD"
        assert payload["quantity"] == 1


# --------------------------------------------------------------------------- #
# Tastytrade: place_order against a stubbed httpx transport
# --------------------------------------------------------------------------- #
class _StubPostResponse:
    def __init__(
        self,
        status_code: int,
        body: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = str(body or "")

    def json(self) -> dict[str, Any]:
        return self._body


class _StubAsyncClient:
    """Minimal async-context-manager stub matching httpx.AsyncClient."""

    def __init__(
        self,
        *,
        post_response: _StubPostResponse | None = None,
        get_response: _StubPostResponse | None = None,
        delete_status: int = 200,
    ) -> None:
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self._post_response = post_response
        self._get_response = get_response
        self._delete_status = delete_status

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> _StubPostResponse:
        _ = headers
        self.post_calls.append((url, json))
        if self._post_response is None:
            return _StubPostResponse(500, {})
        return self._post_response

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
    ) -> _StubPostResponse:
        _ = headers
        self.get_calls.append(url)
        if self._get_response is None:
            return _StubPostResponse(404, {})
        return self._get_response

    async def delete(
        self,
        url: str,
        *,
        headers: dict[str, str],
    ) -> _StubPostResponse:
        _ = headers
        self.delete_calls.append(url)
        return _StubPostResponse(self._delete_status, {})


def _tastytrade_venue() -> TastytradeVenue:
    cfg = TastytradeConfig(
        base_url="https://api.cert.tastyworks.com",
        account_number="5WT99999",
        session_token="cert-token-xyz",
    )
    return TastytradeVenue(cfg)


class TestTastytradePaperExecution:
    def test_missing_creds_returns_rejected(self) -> None:
        venue = TastytradeVenue(TastytradeConfig())
        req = OrderRequest(symbol="BTCUSD", side=Side.BUY, qty=1.0)
        result = asyncio.run(venue.place_order(req))
        assert result.status == OrderStatus.REJECTED
        assert "missing Tastytrade" in result.raw["reason"]

    def test_place_order_with_server_round_trip(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        venue = _tastytrade_venue()
        server_body = {
            "data": {
                "order": {
                    "id": "srv-12345",
                    "status": "Routed",
                    "filled-quantity": 0,
                    "average-fill-price": 0,
                },
            },
        }
        stub = _StubAsyncClient(post_response=_StubPostResponse(201, server_body))

        # Inject stub into the venue's lazy httpx import.
        import httpx as _real_httpx  # noqa: PLC0415 -- ensure module exists

        monkeypatch.setattr(_real_httpx, "AsyncClient", lambda *a, **kw: stub)

        req = OrderRequest(
            symbol="BTCUSD",
            side=Side.BUY,
            qty=1.0,
            order_type=OrderType.MARKET,
        )
        result = asyncio.run(venue.place_order(req))
        assert result.order_id == "srv-12345"
        assert result.status == OrderStatus.OPEN  # "Routed" -> OPEN
        assert stub.post_calls, "expected one POST to the cert API"
        posted_url, posted_body = stub.post_calls[0]
        assert posted_url.endswith("/accounts/5WT99999/orders")
        assert posted_body["order-type"] == "Market"
        assert posted_body["legs"][0]["symbol"] == "/BTCUSD"

    def test_place_order_degrades_on_transport_error(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        venue = _tastytrade_venue()
        # httpx import fails -> _post_order returns None -> mock fallback.
        import builtins  # noqa: PLC0415

        orig_import = builtins.__import__

        def _blocked(name: str, *a: Any, **kw: Any) -> Any:
            if name == "httpx":
                raise ImportError("httpx disabled for this test")
            return orig_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _blocked)

        req = OrderRequest(symbol="BTCUSD", side=Side.BUY, qty=1.0)
        result = asyncio.run(venue.place_order(req))
        assert result.status == OrderStatus.OPEN
        assert result.raw["note"] == "mock_fallback_no_transport_or_network_error"

    def test_place_order_degrades_on_non_2xx(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        venue = _tastytrade_venue()
        # API returns 500 -> _post_order returns None -> mock fallback.
        stub = _StubAsyncClient(post_response=_StubPostResponse(500, {"error": "oops"}))
        import httpx as _real_httpx  # noqa: PLC0415

        monkeypatch.setattr(_real_httpx, "AsyncClient", lambda *a, **kw: stub)

        req = OrderRequest(symbol="BTCUSD", side=Side.BUY, qty=1.0)
        result = asyncio.run(venue.place_order(req))
        assert result.status == OrderStatus.OPEN
        assert "mock_fallback" in result.raw["note"]

    def test_get_order_status_reflects_server_filled(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        venue = _tastytrade_venue()
        filled = {
            "data": {
                "order": {
                    "id": "srv-999",
                    "status": "Filled",
                    "filled-quantity": 1,
                    "average-fill-price": 91234.5,
                },
            },
        }
        stub = _StubAsyncClient(get_response=_StubPostResponse(200, filled))
        import httpx as _real_httpx  # noqa: PLC0415

        monkeypatch.setattr(_real_httpx, "AsyncClient", lambda *a, **kw: stub)

        result = asyncio.run(venue.get_order_status("BTCUSD", "srv-999"))
        assert result is not None
        assert result.status == OrderStatus.FILLED
        assert result.filled_qty == 1.0
        assert result.avg_price == 91234.5

    def test_reconcile_batches_multiple_orders(
        self,
        monkeypatch,  # type: ignore[no-untyped-def]
    ) -> None:
        venue = _tastytrade_venue()
        # Stub returns FILLED for every GET
        stub = _StubAsyncClient(
            get_response=_StubPostResponse(
                200,
                {
                    "data": {
                        "order": {
                            "id": "srv-1",
                            "status": "Filled",
                            "filled-quantity": 1,
                            "average-fill-price": 90000.0,
                        },
                    },
                },
            )
        )
        import httpx as _real_httpx  # noqa: PLC0415

        monkeypatch.setattr(_real_httpx, "AsyncClient", lambda *a, **kw: stub)

        results = asyncio.run(venue.reconcile_orders(["a", "b", "c"]))
        assert len(results) == 3
        assert all(r.status == OrderStatus.FILLED for r in results)
        assert len(stub.get_calls) == 3


# --------------------------------------------------------------------------- #
# Status mapping
# --------------------------------------------------------------------------- #
class TestTastytradeStatusMap:
    def test_filled(self) -> None:
        assert _map_tasty_status("Filled") == OrderStatus.FILLED

    def test_partial(self) -> None:
        assert _map_tasty_status("Partial Filled") == OrderStatus.PARTIAL
        assert _map_tasty_status("Partially Filled") == OrderStatus.PARTIAL

    def test_rejected_variants(self) -> None:
        assert _map_tasty_status("Rejected") == OrderStatus.REJECTED
        assert _map_tasty_status("Cancelled") == OrderStatus.REJECTED
        assert _map_tasty_status("Canceled") == OrderStatus.REJECTED
        assert _map_tasty_status("Expired") == OrderStatus.REJECTED

    def test_open_variants(self) -> None:
        for raw in ("Routed", "Received", "Live", "In Flight", "Replaced"):
            assert _map_tasty_status(raw) == OrderStatus.OPEN

    def test_unknown_defaults_to_open(self) -> None:
        assert _map_tasty_status("NoveltyStatus") == OrderStatus.OPEN
        assert _map_tasty_status(None) == OrderStatus.OPEN
