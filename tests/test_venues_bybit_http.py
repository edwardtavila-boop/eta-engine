"""
EVOLUTIONARY TRADING ALGO  //  tests.test_venues_bybit_http
===============================================
Integration tests for the aiohttp-backed paths in BybitVenue.
Uses an injected fake session (no network).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from eta_engine.venues.base import (
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from eta_engine.venues.bybit import BybitVenue


# --------------------------------------------------------------------------- #
# Fake aiohttp session
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:  # noqa: ANN401 - body may be dict / list / str / bytes
        self.status = status
        self._body = body

    async def text(self) -> str:
        if isinstance(self._body, (dict, list)):
            return json.dumps(self._body)
        return str(self._body)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[_FakeResponse] = []
        self.closed: bool = False

    def enqueue(self, status: int, body: Any) -> None:  # noqa: ANN401 - body may be dict / list / str / bytes
        self._queue.append(_FakeResponse(status, body))

    def _next(self) -> _FakeResponse:
        if self._queue:
            return self._queue.pop(0)
        return _FakeResponse(200, {"retCode": 0})

    def post(self, url: str, data: str = "", headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append({"method": "POST", "url": url, "data": data, "headers": headers or {}})
        return self._next()

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, "data": None, "headers": headers or {}})
        return self._next()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def creds_venue() -> BybitVenue:
    return BybitVenue(api_key="KEY", api_secret="SECRET", testnet=True)


@pytest.fixture()
def fake_session() -> _FakeSession:
    return _FakeSession()


# --------------------------------------------------------------------------- #
# place_order
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_place_order_http_success(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"orderId": "4e7f6d2d", "orderLinkId": "link-1"},
        },
    )
    req = OrderRequest(symbol="ETHUSDT", side=Side.BUY, qty=0.5)
    res = await creds_venue.place_order(req)
    assert res.status is OrderStatus.OPEN
    assert res.order_id == "4e7f6d2d"

    call = fake_session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v5/order/create")
    assert "api-testnet.bybit.com" in call["url"]
    # Signed headers present
    assert "X-BAPI-SIGN" in call["headers"]
    assert "X-BAPI-API-KEY" in call["headers"]
    payload = json.loads(call["data"])
    assert payload["category"] == "linear"
    assert payload["symbol"] == "ETHUSDT"
    assert payload["side"] == "Buy"


@pytest.mark.asyncio
async def test_place_order_retcode_nonzero_is_rejected(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "retCode": 110007,
            "retMsg": "insufficient balance",
            "result": {"orderLinkId": "link-2"},
        },
    )
    req = OrderRequest(symbol="ETHUSDT", side=Side.BUY, qty=999)
    res = await creds_venue.place_order(req)
    assert res.status is OrderStatus.REJECTED
    assert res.raw["retMsg"] == "insufficient balance"


@pytest.mark.asyncio
async def test_place_order_http_5xx_is_rejected(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(503, {"error": "service unavailable"})
    fake_session.enqueue(503, {"error": "service unavailable"})  # retry also 503
    req = OrderRequest(symbol="ETHUSDT", side=Side.BUY, qty=0.5)
    res = await creds_venue.place_order(req)
    assert res.status is OrderStatus.REJECTED
    assert res.raw["http_status"] == 503


@pytest.mark.asyncio
async def test_place_order_no_creds_returns_mock() -> None:
    v = BybitVenue()
    # No session needed — mock path doesn't hit HTTP.
    req = OrderRequest(symbol="ETHUSDT", side=Side.BUY, qty=0.1)
    res = await v.place_order(req)
    assert res.status is OrderStatus.OPEN
    assert res.raw["retCode"] == 0


# --------------------------------------------------------------------------- #
# cancel_order
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_order_http_success(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"retCode": 0, "retMsg": "OK", "result": {}})
    ok = await creds_venue.cancel_order("ETHUSDT", "order-xyz")
    assert ok is True
    call = fake_session.calls[-1]
    assert call["url"].endswith("/v5/order/cancel")
    body = json.loads(call["data"])
    assert body["symbol"] == "ETHUSDT"
    assert body["orderId"] == "order-xyz"


@pytest.mark.asyncio
async def test_cancel_order_retcode_nonzero_returns_false(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"retCode": 110001, "retMsg": "order not exist"})
    ok = await creds_venue.cancel_order("ETHUSDT", "ghost")
    assert ok is False


# --------------------------------------------------------------------------- #
# get_positions / get_balance
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_positions_parses_list(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "retCode": 0,
            "result": {"list": [{"symbol": "ETHUSDT", "size": "0.5"}]},
        },
    )
    positions = await creds_venue.get_positions("ETHUSDT")
    assert positions == [{"symbol": "ETHUSDT", "size": "0.5"}]
    assert "/v5/position/list" in fake_session.calls[-1]["url"]
    assert "symbol=ETHUSDT" in fake_session.calls[-1]["url"]


@pytest.mark.asyncio
async def test_get_balance_sums_unified_coin(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "coin": [
                            {"coin": "USDT", "walletBalance": "1234.56"},
                            {"coin": "BTC", "walletBalance": "0.1"},
                        ]
                    }
                ]
            },
        },
    )
    bal = await creds_venue.get_balance("USDT")
    assert bal == {"USDT": 1234.56}


@pytest.mark.asyncio
async def test_get_balance_retcode_failure_returns_zero(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"retCode": 10001, "retMsg": "nope"})
    bal = await creds_venue.get_balance("USDT")
    assert bal == {"USDT": 0.0}


# --------------------------------------------------------------------------- #
# leverage / isolated
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_set_leverage_posts_payload(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"retCode": 0})
    ok = await creds_venue.set_leverage("ETHUSDT", 5)
    assert ok is True
    body = json.loads(fake_session.calls[-1]["data"])
    assert body["buyLeverage"] == "5"
    assert body["sellLeverage"] == "5"


@pytest.mark.asyncio
async def test_set_isolated_margin_treats_already_isolated_as_success(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"retCode": 110026, "retMsg": "already isolated"})
    ok = await creds_venue.set_isolated_margin("ETHUSDT")
    assert ok is True


# --------------------------------------------------------------------------- #
# session lifecycle + limit-type order
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_close_closes_session(creds_venue: BybitVenue, fake_session: _FakeSession) -> None:
    creds_venue._session = fake_session
    await creds_venue.close()
    assert fake_session.closed is True
    assert creds_venue._session is None


@pytest.mark.asyncio
async def test_place_limit_order_includes_price(
    creds_venue: BybitVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"orderId": "lim-1", "orderLinkId": "lnk"},
        },
    )
    req = OrderRequest(
        symbol="ETHUSDT",
        side=Side.BUY,
        qty=0.1,
        order_type=OrderType.LIMIT,
        price=2500.0,
    )
    res = await creds_venue.place_order(req)
    assert res.status is OrderStatus.OPEN
    body = json.loads(fake_session.calls[-1]["data"])
    assert body["price"] == "2500.0"
    assert body["orderType"] == "Limit"
    assert body["timeInForce"] == "GTC"
