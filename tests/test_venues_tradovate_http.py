"""
EVOLUTIONARY TRADING ALGO  //  tests.test_venues_tradovate_http
===================================================
Integration tests for the real aiohttp-backed paths in TradovateVenue.
We don't hit the live API; we inject a fake session that captures the
requested URL/body/headers and returns canned responses.
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
from eta_engine.venues.tradovate import TradovateVenue


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
    """
    Minimal async session. Each POST/GET records the call and returns a
    queued _FakeResponse (or a default 200 OK empty).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[_FakeResponse] = []
        self.closed: bool = False
        self._fail_times: int = 0  # force N network failures before success

    def enqueue(self, status: int, body: Any) -> None:  # noqa: ANN401 - body may be dict / list / str / bytes
        self._queue.append(_FakeResponse(status, body))

    def enqueue_network_error(self, n: int = 1) -> None:
        self._fail_times += n

    def _next_response(self) -> _FakeResponse:
        if self._queue:
            return self._queue.pop(0)
        return _FakeResponse(200, {})

    def post(self, url: str, data: str = "", headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append({"method": "POST", "url": url, "data": data, "headers": headers or {}})
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("simulated transient network error")
        return self._next_response()

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, "data": None, "headers": headers or {}})
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("simulated transient network error")
        return self._next_response()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def creds_venue() -> TradovateVenue:
    """Tradovate configured with non-empty creds so HTTP branch fires."""
    v = TradovateVenue(api_key="user@example.com", api_secret="s3cret", demo=True, cid="12345")
    return v


@pytest.fixture()
def fake_session() -> _FakeSession:
    return _FakeSession()


# --------------------------------------------------------------------------- #
# authenticate()
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_authenticate_posts_credentials_and_sets_token(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(
        200,
        {
            "accessToken": "REAL-TOKEN-123",
            "mdAccessToken": "MD-TOKEN-456",
            "expirationTime": "2099-01-01T00:00:00.000Z",
        },
    )
    await creds_venue.authenticate()

    assert creds_venue._access_token == "REAL-TOKEN-123"
    assert creds_venue._md_access_token == "MD-TOKEN-456"
    assert creds_venue._expiration is not None
    assert creds_venue._expiration.year == 2099

    assert len(fake_session.calls) == 1
    call = fake_session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/auth/accessTokenRequest")
    payload = json.loads(call["data"])
    assert payload["name"] == "user@example.com"
    assert payload["password"] == "s3cret"
    assert payload["cid"] == "12345"
    assert call["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_authenticate_raises_on_non_200(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(401, {"errorText": "bad creds"})
    with pytest.raises(RuntimeError, match="tradovate authenticate failed"):
        await creds_venue.authenticate()


@pytest.mark.asyncio
async def test_authenticate_raises_when_accesstoken_missing(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"p-ticket": "captcha-needed"})
    with pytest.raises(RuntimeError):
        await creds_venue.authenticate()


@pytest.mark.asyncio
async def test_authenticate_stub_path_when_no_creds() -> None:
    """Empty creds -> no HTTP, stub token, no exceptions."""
    v = TradovateVenue()  # empty creds
    # No session attached -- should NOT make HTTP.
    await v.authenticate()
    assert v._access_token == "stub-access-token"
    assert v._expiration is not None


@pytest.mark.asyncio
async def test_authenticate_sends_distinct_app_secret_when_provided() -> None:
    """When app_secret differs from api_secret, `sec` field reflects app_secret.

    Guards against reusing the user's account password for the API-app secret
    (Tradovate treats `password` and `sec` as independent per
    /auth/accessTokenRequest docs).
    """
    v = TradovateVenue(
        api_key="user@example.com",
        api_secret="account-password",
        demo=True,
        cid="99999",
        app_secret="DIFFERENT-APP-SECRET",
    )
    session = _FakeSession()
    v._session = session
    session.enqueue(
        200,
        {
            "accessToken": "TOK",
            "mdAccessToken": "MD",
            "expirationTime": "2099-01-01T00:00:00Z",
        },
    )
    await v.authenticate()
    payload = json.loads(session.calls[0]["data"])
    assert payload["password"] == "account-password"
    assert payload["sec"] == "DIFFERENT-APP-SECRET"
    assert payload["sec"] != payload["password"]


@pytest.mark.asyncio
async def test_authenticate_falls_back_sec_to_api_secret_when_app_secret_empty() -> None:
    """Backward-compat: callers who don't pass app_secret still get a working payload."""
    v = TradovateVenue(
        api_key="user@example.com",
        api_secret="shared-secret",
        demo=True,
        cid="1",
    )
    session = _FakeSession()
    v._session = session
    session.enqueue(200, {"accessToken": "TOK", "expirationTime": "2099-01-01T00:00:00Z"})
    await v.authenticate()
    payload = json.loads(session.calls[0]["data"])
    assert payload["sec"] == "shared-secret"
    assert payload["password"] == "shared-secret"


# --------------------------------------------------------------------------- #
# place_order()
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_place_order_http_success_returns_open_with_orderid(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    # First call: authenticate. Second: place.
    fake_session.enqueue(200, {"accessToken": "T", "expirationTime": "2099-01-01T00:00:00Z"})
    fake_session.enqueue(200, {"orderId": 987654, "clOrdId": "abc"})

    req = OrderRequest(symbol="MNQ", side=Side.BUY, qty=1)
    res = await creds_venue.place_order(req)

    assert res.order_id == "987654"
    assert res.status is OrderStatus.OPEN
    # Authenticate + place
    assert [c["url"].split("/")[-1] for c in fake_session.calls] == [
        "accessTokenRequest",
        "placeOrder",
    ]
    # The place request should carry a Bearer header + resolved futures symbol
    place_call = fake_session.calls[1]
    assert place_call["headers"]["Authorization"].startswith("Bearer ")
    body = json.loads(place_call["data"])
    assert body["symbol"].startswith("MNQ") and body["symbol"][3] in {"H", "M", "U", "Z"}
    assert body["action"] == "Buy"
    assert body["orderQty"] == 1
    assert body["isAutomated"] is True
    assert "clOrdId" in body


@pytest.mark.asyncio
async def test_place_order_http_rejection_returns_rejected(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    fake_session.enqueue(200, {"accessToken": "T", "expirationTime": "2099-01-01T00:00:00Z"})
    fake_session.enqueue(400, {"errorText": "insufficient margin"})

    req = OrderRequest(symbol="MNQ", side=Side.BUY, qty=99)
    res = await creds_venue.place_order(req)

    assert res.status is OrderStatus.REJECTED
    assert res.raw.get("http_status") == 400
    assert "insufficient margin" in res.raw.get("errorText", "")


@pytest.mark.asyncio
async def test_place_order_retries_on_transient_network_error(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    # One network failure then success on auth, then success on place.
    fake_session.enqueue_network_error(1)
    fake_session.enqueue(200, {"accessToken": "T", "expirationTime": "2099-01-01T00:00:00Z"})
    fake_session.enqueue(200, {"orderId": 111, "clOrdId": "x"})

    req = OrderRequest(symbol="MNQ", side=Side.BUY, qty=1)
    res = await creds_venue.place_order(req)

    assert res.status is OrderStatus.OPEN
    assert res.order_id == "111"


# --------------------------------------------------------------------------- #
# cancel_order()
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_order_posts_orderid(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    # Pre-seed token so cancel doesn't try to auth
    creds_venue._access_token = "SEEDED"
    from datetime import UTC, datetime, timedelta

    creds_venue._expiration = datetime.now(UTC) + timedelta(hours=1)
    fake_session.enqueue(200, {"ok": True})

    ok = await creds_venue.cancel_order("MNQM6", "42")
    assert ok is True
    assert fake_session.calls[-1]["url"].endswith("/order/cancelOrder")
    body = json.loads(fake_session.calls[-1]["data"])
    assert body == {"orderId": 42}


@pytest.mark.asyncio
async def test_cancel_order_returns_false_on_non_200(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    from datetime import UTC, datetime, timedelta

    creds_venue._access_token = "T"
    creds_venue._expiration = datetime.now(UTC) + timedelta(hours=1)
    fake_session.enqueue(500, {"error": "server"})

    ok = await creds_venue.cancel_order("MNQM6", "42")
    assert ok is False


# --------------------------------------------------------------------------- #
# get_positions() + get_balance()
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_positions_returns_parsed_list(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    from datetime import UTC, datetime, timedelta

    creds_venue._access_token = "T"
    creds_venue._expiration = datetime.now(UTC) + timedelta(hours=1)
    positions = [{"id": 1, "symbol": "MNQM6", "netPos": 1}]
    fake_session.enqueue(200, positions)

    got = await creds_venue.get_positions()
    assert got == positions
    assert fake_session.calls[-1]["method"] == "GET"
    assert fake_session.calls[-1]["url"].endswith("/position/list")


@pytest.mark.asyncio
async def test_get_positions_returns_empty_on_bad_payload(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    from datetime import UTC, datetime, timedelta

    creds_venue._access_token = "T"
    creds_venue._expiration = datetime.now(UTC) + timedelta(hours=1)
    fake_session.enqueue(200, {"not": "a list"})
    got = await creds_venue.get_positions()
    assert got == []


@pytest.mark.asyncio
async def test_get_balance_sums_amount_fields(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    from datetime import UTC, datetime, timedelta

    creds_venue._access_token = "T"
    creds_venue._expiration = datetime.now(UTC) + timedelta(hours=1)
    fake_session.enqueue(
        200,
        [
            {"id": 1, "amount": 2500.0},
            {"id": 2, "amount": 1500.0},
            {"id": 3, "amount": "not-a-number"},  # should be skipped gracefully
        ],
    )
    bal = await creds_venue.get_balance()
    assert bal == {"USD": 4000.0}


@pytest.mark.asyncio
async def test_get_balance_zero_when_no_creds() -> None:
    v = TradovateVenue()  # no creds
    # _ensure_token runs the stub path (sets token); no HTTP attempted.
    bal = await v.get_balance()
    assert bal == {"USD": 0.0}


# --------------------------------------------------------------------------- #
# bracket_order()
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bracket_order_http_success_returns_three_legs(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    from datetime import UTC, datetime, timedelta

    creds_venue._access_token = "T"
    creds_venue._expiration = datetime.now(UTC) + timedelta(hours=1)
    fake_session.enqueue(200, {"orderId": 5000, "clOrdId": "oso-xyz"})

    req = OrderRequest(symbol="MNQ", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
    legs = await creds_venue.bracket_order(req, stop_price=20_000.0, target_price=20_100.0)

    assert len(legs) == 3
    entry, stop, target = legs
    assert entry.order_id == "5000"
    assert entry.status is OrderStatus.OPEN
    assert stop.avg_price == 20_000.0
    assert target.avg_price == 20_100.0
    assert fake_session.calls[-1]["url"].endswith("/order/placeOSO")
    body = json.loads(fake_session.calls[-1]["data"])
    assert body["entry"]["action"] == "Buy"
    assert body["brackets"][0]["orderType"] == "Stop"
    assert body["brackets"][1]["orderType"] == "Limit"


@pytest.mark.asyncio
async def test_bracket_order_http_failure_returns_single_rejected_leg(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    from datetime import UTC, datetime, timedelta

    creds_venue._access_token = "T"
    creds_venue._expiration = datetime.now(UTC) + timedelta(hours=1)
    fake_session.enqueue(400, {"errorText": "invalid stop"})

    req = OrderRequest(symbol="MNQ", side=Side.BUY, qty=1)
    legs = await creds_venue.bracket_order(req, stop_price=0, target_price=0)

    assert len(legs) == 1
    assert legs[0].status is OrderStatus.REJECTED


# --------------------------------------------------------------------------- #
# session lifecycle
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_close_closes_session(
    creds_venue: TradovateVenue,
    fake_session: _FakeSession,
) -> None:
    creds_venue._session = fake_session
    await creds_venue.close()
    assert fake_session.closed is True
    assert creds_venue._session is None


@pytest.mark.asyncio
async def test_close_is_safe_when_no_session() -> None:
    v = TradovateVenue()
    # Should not raise
    await v.close()
    assert v._session is None
