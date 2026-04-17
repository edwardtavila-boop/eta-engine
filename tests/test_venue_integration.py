"""
EVOLUTIONARY TRADING ALGO  //  tests.test_venue_integration
===============================================
End-to-end adapter wiring: payload structure, bracket legs, router dispatch.
HTTP is mocked (no network). Verifies request shapes match exchange specs.
"""

from __future__ import annotations

import pytest

from eta_engine.venues import (
    BybitVenue,
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
            symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.5,
            order_type=OrderType.LIMIT, price=3500.0,
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

    def test_bybit_parse_rejected_response(self) -> None:
        venue = BybitVenue()
        raw = {"retCode": 10001, "retMsg": "params error", "result": {}, "retExtInfo": {}, "time": 1}
        parsed = venue._parse_order_response(raw, fallback_id="fb")
        assert parsed.status is OrderStatus.REJECTED
        assert parsed.raw["retCode"] == 10001


class TestTradovateBracket:

    @pytest.mark.asyncio
    async def test_tradovate_bracket_structure(self) -> None:
        # No creds -> stub path (exercises leg-structure logic, no HTTP).
        venue = TradovateVenue(demo=True)
        entry = OrderRequest(
            symbol="MNQM6", side=Side.BUY, qty=1,
            order_type=OrderType.LIMIT, price=21550.0,
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

    def test_router_picks_tradovate_for_futures(self) -> None:
        router = SmartRouter()
        assert router.choose_venue("MNQM6", 1) is router.tradovate
        assert router.choose_venue("NQU6", 1) is router.tradovate
        assert router.choose_venue("ESH6", 1) is router.tradovate

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
