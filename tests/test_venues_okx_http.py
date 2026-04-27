"""
EVOLUTIONARY TRADING ALGO  //  tests.test_venues_okx_http
=============================================
Integration tests for the aiohttp-backed paths in OkxVenue.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest

from eta_engine.venues.base import (
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from eta_engine.venues.okx import OkxVenue


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
        return _FakeResponse(200, {"code": "0"})

    def post(self, url: str, data: str = "", headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append({"method": "POST", "url": url, "data": data, "headers": headers or {}})
        return self._next()

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, "data": None, "headers": headers or {}})
        return self._next()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def creds_venue() -> OkxVenue:
    return OkxVenue(api_key="KEY", api_secret="SECRET", passphrase="PASS")


@pytest.fixture()
def fake_session() -> _FakeSession:
    return _FakeSession()


# --------------------------------------------------------------------------- #
# Signing spec compliance
# --------------------------------------------------------------------------- #
def test_sign_matches_okx_spec() -> None:
    venue = OkxVenue(api_key="K", api_secret="S", passphrase="P")
    ts = "2026-04-16T12:00:00.000Z"
    sig = venue._sign(ts, "POST", "/api/v5/trade/order", '{"a":1}')
    expected_prehash = f"{ts}POST/api/v5/trade/order" + '{"a":1}'
    expected = base64.b64encode(
        hmac.new(b"S", expected_prehash.encode(), hashlib.sha256).digest(),
    ).decode()
    assert sig == expected


def test_symbol_mapping_standard() -> None:
    v = OkxVenue()
    assert v._native_symbol("ETH/USDT:USDT") == "ETH-USDT-SWAP"
    assert v._native_symbol("BTC/USDT:USDT") == "BTC-USDT-SWAP"
    # Passthrough for already-native
    assert v._native_symbol("SOL-USDT-SWAP") == "SOL-USDT-SWAP"
    # Generic CCXT fallback
    assert v._native_symbol("LINK/USDT:USDT") == "LINK-USDT-SWAP"


# --------------------------------------------------------------------------- #
# place_order
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_place_order_http_success(
    creds_venue: OkxVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "code": "0",
            "msg": "",
            "data": [{"ordId": "OKX-99", "clOrdId": "abc", "sCode": "0", "sMsg": ""}],
        },
    )
    req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.5)
    res = await creds_venue.place_order(req)
    assert res.status is OrderStatus.OPEN
    assert res.order_id == "OKX-99"

    call = fake_session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/v5/trade/order")
    body = json.loads(call["data"])
    assert body["instId"] == "ETH-USDT-SWAP"
    assert body["side"] == "buy"
    assert body["ordType"] == "market"
    # Signed headers
    headers = call["headers"]
    assert headers["OK-ACCESS-KEY"] == "KEY"
    assert headers["OK-ACCESS-PASSPHRASE"] == "PASS"
    assert "OK-ACCESS-SIGN" in headers
    assert "OK-ACCESS-TIMESTAMP" in headers


@pytest.mark.asyncio
async def test_place_order_nonzero_code_is_rejected(
    creds_venue: OkxVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"code": "51008", "msg": "insufficient balance", "data": []})
    req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=99)
    res = await creds_venue.place_order(req)
    assert res.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_place_order_http_5xx_is_rejected(
    creds_venue: OkxVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(503, {"error": "busy"})
    fake_session.enqueue(503, {"error": "busy"})
    req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.1)
    res = await creds_venue.place_order(req)
    assert res.status is OrderStatus.REJECTED
    assert res.raw["http_status"] == 503


@pytest.mark.asyncio
async def test_place_order_no_creds_returns_stub() -> None:
    v = OkxVenue()  # no creds
    req = OrderRequest(symbol="ETH/USDT:USDT", side=Side.BUY, qty=0.1)
    res = await v.place_order(req)
    assert res.status is OrderStatus.OPEN
    assert res.raw.get("stub") is True


# --------------------------------------------------------------------------- #
# cancel + positions + balance
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_order_http_success(
    creds_venue: OkxVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"code": "0", "msg": "", "data": []})
    ok = await creds_venue.cancel_order("ETH/USDT:USDT", "X1")
    assert ok is True
    body = json.loads(fake_session.calls[-1]["data"])
    assert body["instId"] == "ETH-USDT-SWAP"
    assert body["ordId"] == "X1"


@pytest.mark.asyncio
async def test_cancel_order_nonzero_code_returns_false(
    creds_venue: OkxVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"code": "51401", "msg": "cancel failed"})
    ok = await creds_venue.cancel_order("ETH/USDT:USDT", "ghost")
    assert ok is False


@pytest.mark.asyncio
async def test_get_positions_parses_list(
    creds_venue: OkxVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "code": "0",
            "data": [{"instId": "ETH-USDT-SWAP", "pos": "0.5"}],
        },
    )
    out = await creds_venue.get_positions()
    assert out == [{"instId": "ETH-USDT-SWAP", "pos": "0.5"}]


@pytest.mark.asyncio
async def test_get_balance_sums_usdt_availbal(
    creds_venue: OkxVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "code": "0",
            "data": [
                {
                    "details": [
                        {"ccy": "USDT", "availBal": "500.25"},
                        {"ccy": "BTC", "availBal": "0.01"},
                    ]
                }
            ],
        },
    )
    bal = await creds_venue.get_balance()
    assert bal == {"USDT": 500.25}


@pytest.mark.asyncio
async def test_get_balance_no_creds_returns_zero() -> None:
    v = OkxVenue()
    bal = await v.get_balance()
    assert bal == {"USDT": 0.0}


@pytest.mark.asyncio
async def test_close_closes_session(creds_venue: OkxVenue, fake_session: _FakeSession) -> None:
    creds_venue._session = fake_session
    await creds_venue.close()
    assert fake_session.closed is True
    assert creds_venue._session is None


@pytest.mark.asyncio
async def test_place_limit_order_includes_price(
    creds_venue: OkxVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "code": "0",
            "data": [{"ordId": "LIM-1"}],
        },
    )
    req = OrderRequest(
        symbol="ETH/USDT:USDT",
        side=Side.BUY,
        qty=0.1,
        order_type=OrderType.LIMIT,
        price=2500.0,
    )
    await creds_venue.place_order(req)
    body = json.loads(fake_session.calls[-1]["data"])
    assert body["px"] == "2500.0"
    assert body["ordType"] == "limit"
