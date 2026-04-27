"""
EVOLUTIONARY TRADING ALGO  //  tests.test_venue_integration
===============================================
End-to-end adapter wiring: payload structure, bracket legs, router dispatch.
HTTP is mocked (no network). Verifies request shapes match exchange specs.
"""

from __future__ import annotations

from typing import Any

import pytest

from eta_engine.venues import (
    BybitVenue,
    OkxVenue,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    SmartRouter,
    TradovateVenue,
)


class TestBybitOrderFlow:
    def test_bybit_place_payload_structure(self) -> None:
        venue = BybitVenue(api_key="K", api_secret="S")
        req = OrderRequest(
            symbol="ETH/USDT:USDT",
            side=Side.BUY,
            qty=0.5,
            order_type=OrderType.LIMIT,
            price=3500.0,
        )
        payload = venue._build_place_payload(req)
        assert payload["category"] == "linear"
        assert payload["symbol"] == "ETHUSDT"
        assert payload["side"] == "Buy"
        assert payload["orderType"] == "Limit"
        assert payload["qty"] == "0.5"
        assert payload["price"] == "3500.0"
        assert payload["timeInForce"] == "GTC"
        assert payload["reduceOnly"] is False
        assert isinstance(payload["orderLinkId"], str)
        assert len(payload["orderLinkId"]) == 32

    def test_bybit_market_payload_omits_price(self) -> None:
        venue = BybitVenue()
        req = OrderRequest(symbol="BTCUSDT", side=Side.SELL, qty=0.1)  # market default
        payload = venue._build_place_payload(req)
        assert payload["orderType"] == "Market"
        assert payload["timeInForce"] == "IOC"
        assert "price" not in payload

    @pytest.mark.asyncio
    async def test_bybit_order_flow_dry_run(self) -> None:
        # No creds -> mock path (safe for CI, no network).
        venue = BybitVenue()
        req = OrderRequest(symbol="ETHUSDT", side=Side.BUY, qty=0.2)
        result = await venue.place_order(req)
        assert result.status is OrderStatus.OPEN
        assert result.raw.get("retCode") == 0
        assert "orderId" in result.raw.get("result", {})

    @pytest.mark.asyncio
    async def test_bybit_order_status_round_trip_from_mock_cache(self) -> None:
        venue = BybitVenue()
        req = OrderRequest(symbol="ETHUSDT", side=Side.BUY, qty=0.2)
        placed = await venue.place_order(req)
        assert placed.status is OrderStatus.OPEN

        cached = await venue.get_order_status("ETHUSDT", placed.order_id)
        assert cached is not None
        assert cached.status is OrderStatus.OPEN
        assert cached.raw["venue"] == "bybit"

        venue._mock_orders[placed.order_id] = venue._mock_orders[placed.order_id].model_copy(  # noqa: SLF001
            update={
                "status": OrderStatus.FILLED,
                "filled_qty": 0.2,
                "avg_price": 3500.0,
                "fees": 0.01,
                "raw": {
                    **venue._mock_orders[placed.order_id].raw,  # noqa: SLF001
                    "orderStatus": "Filled",
                    "cumExecQty": "0.2",
                    "leavesQty": "0",
                    "avgPrice": "3500.0",
                    "cumExecFee": "0.01",
                },
            },
        )

        filled = await venue.get_order_status("ETHUSDT", placed.order_id)
        assert filled is not None
        assert filled.status is OrderStatus.FILLED
        assert filled.filled_qty == pytest.approx(0.2)
        assert filled.avg_price == pytest.approx(3500.0)
        assert filled.fees == pytest.approx(0.01)
        assert filled.raw["venue"] == "bybit"

    def test_bybit_parse_rejected_response(self) -> None:
        venue = BybitVenue()
        raw = {"retCode": 10001, "retMsg": "params error", "result": {}, "retExtInfo": {}, "time": 1}
        parsed = venue._parse_order_response(raw, fallback_id="fb")
        assert parsed.status is OrderStatus.REJECTED
        assert parsed.raw["retCode"] == 10001

    @pytest.mark.asyncio
    async def test_bybit_order_book_snapshot_is_normalized(self) -> None:
        venue = BybitVenue()

        async def _fake_http_get(path: str, qs: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            assert path == "/v5/market/orderbook"
            assert "category=linear" in qs
            assert "symbol=BTCUSDT" in qs
            return (
                200,
                {
                    "retCode": 0,
                    "retMsg": "",
                    "result": {
                        "ts": "1713434102752",
                        "b": [["60000.0", "2.5"], ["59999.5", "1.5"], ["59999.0", "1.0"]],
                        "a": [["60000.5", "2.0"], ["60001.0", "1.0"], ["60001.5", "0.5"]],
                    },
                    "time": 1713434102753,
                },
            )

        venue._http_get = _fake_http_get  # type: ignore[method-assign]
        book = await venue.get_order_book("BTCUSDT", depth=3)
        assert book is not None
        assert book["venue"] == "bybit"
        assert book["order_book_venue"] == "bybit"
        assert book["order_book_depth"] == 3
        assert book["best_bid"] == pytest.approx(60000.0)
        assert book["best_ask"] == pytest.approx(60000.5)
        assert book["spread_bps"] == pytest.approx((0.5 / 60000.25) * 10_000.0)
        assert book["book_imbalance"] == pytest.approx((5.0 - 3.5) / (5.0 + 3.5))
        assert book["spread_regime"] == "TIGHT"


class TestOkxOrderBook:
    @pytest.mark.asyncio
    async def test_okx_order_book_snapshot_is_normalized(self) -> None:
        venue = OkxVenue()

        async def _fake_http_get(
            request_path: str,
            qs: str = "",
            headers: dict[str, str] | None = None,
        ) -> tuple[int, dict[str, Any]]:
            assert request_path == "/api/v5/market/books"
            assert "instId=BTC-USDT-SWAP" in qs
            return (
                200,
                {
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "ts": "1713434102752",
                            "asks": [["60000.5", "2.0", "0", "1"], ["60001.0", "1.0", "0", "2"]],
                            "bids": [["60000.0", "2.5", "0", "1"], ["59999.5", "1.5", "0", "2"]],
                        }
                    ],
                },
            )

        venue._http_get = _fake_http_get  # type: ignore[method-assign]
        book = await venue.get_order_book("BTC/USDT:USDT", depth=2)
        assert book is not None
        assert book["venue"] == "okx"
        assert book["order_book_venue"] == "okx"
        assert book["order_book_depth"] == 2
        assert book["best_bid"] == pytest.approx(60000.0)
        assert book["best_ask"] == pytest.approx(60000.5)
        assert book["spread_regime"] == "TIGHT"


class TestTradovateBracket:
    @pytest.mark.asyncio
    async def test_tradovate_bracket_structure(self) -> None:
        # No creds -> stub path (exercises leg-structure logic, no HTTP).
        venue = TradovateVenue(demo=True)
        entry = OrderRequest(
            symbol="MNQM6",
            side=Side.BUY,
            qty=1,
            order_type=OrderType.LIMIT,
            price=21550.0,
        )
        legs = await venue.bracket_order(entry, stop_price=21500.0, target_price=21650.0)
        assert isinstance(legs, list)
        assert len(legs) == 3
        parent_id = legs[0].order_id
        assert parent_id.startswith("oso-")
        # Stop + target must reference parent
        assert legs[1].order_id.endswith("-S")
        assert legs[2].order_id.endswith("-T")
        assert legs[1].raw.get("parent") == parent_id
        assert legs[2].raw.get("parent") == parent_id
        assert legs[1].avg_price == 21500.0
        assert legs[2].avg_price == 21650.0

    @pytest.mark.asyncio
    async def test_tradovate_authenticates_before_order(self) -> None:
        # No creds -> stub auth (no HTTP). Still exercises the
        # `_ensure_token` -> `authenticate` call ordering.
        venue = TradovateVenue()
        assert venue._access_token is None
        req = OrderRequest(symbol="MNQM6", side=Side.BUY, qty=1)
        await venue.place_order(req)
        assert venue._access_token is not None
        assert venue._expiration is not None


class TestRouterDispatch:
    def test_router_picks_bybit_for_crypto(self) -> None:
        router = SmartRouter()
        assert router.choose_venue("ETHUSDT", 0.5) is router.bybit
        assert router.choose_venue("BTC/USDT:USDT", 0.1) is router.bybit
        assert router.choose_venue("SOLUSDT", 10) is router.bybit

    def test_router_picks_ibkr_for_futures_by_default(self) -> None:
        # Broker dormancy mandate 2026-04-24: IBKR is the active default
        # futures venue while Tradovate is funding-blocked. See
        # eta_engine.venues.router.DORMANT_BROKERS.
        router = SmartRouter()
        assert router.choose_venue("MNQM6", 1) is router.ibkr
        assert router.choose_venue("NQU6", 1) is router.ibkr
        assert router.choose_venue("ESH6", 1) is router.ibkr

    @pytest.mark.asyncio
    async def test_router_failover_records_log(self) -> None:
        from eta_engine.venues import OkxVenue
        from eta_engine.venues.base import OrderResult

        class _BoomBybit(BybitVenue):
            async def place_order(self, request: OrderRequest) -> OrderResult:
                raise ConnectionError("simulated net outage")

        class _OkOkx(OkxVenue):
            async def place_order(self, request: OrderRequest) -> OrderResult:
                return OrderResult(order_id="okx-ok", status=OrderStatus.OPEN)

        router = SmartRouter(bybit=_BoomBybit(), okx=_OkOkx())
        req = OrderRequest(symbol="ETHUSDT", side=Side.BUY, qty=0.5)
        result = await router.place_with_failover(req)
        assert result.order_id == "okx-ok"
        assert any(e["reason"] == "exception" for e in router._failover_log)

    @pytest.mark.asyncio
    async def test_router_order_book_falls_back_to_secondary(self) -> None:
        class _NoBookBybit(BybitVenue):
            async def get_order_book(self, symbol: str, depth: int = 5) -> dict[str, Any] | None:
                return None

        class _BookOkx(OkxVenue):
            async def get_order_book(self, symbol: str, depth: int = 5) -> dict[str, Any] | None:
                _ = depth
                return {
                    "venue": "okx",
                    "symbol": symbol,
                    "order_book_depth": 5,
                    "best_bid": 60_000.0,
                    "best_ask": 60_000.5,
                    "bid_price": 60_000.0,
                    "ask_price": 60_000.5,
                    "bid_depth": 8.0,
                    "ask_depth": 6.0,
                    "spread": 0.5,
                    "spread_bps": 0.0833,
                    "book_imbalance": 0.1428571429,
                    "spread_regime": "TIGHT",
                }

        router = SmartRouter(bybit=_NoBookBybit(), okx=_BookOkx())
        book = await router.get_order_book("BTCUSDT")
        assert book is not None
        assert book["venue"] == "okx"
        assert book["order_book_venue"] == "okx"
        assert book["order_book_depth"] == 5
        assert book["spread_regime"] == "TIGHT"
