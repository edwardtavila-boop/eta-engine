"""
EVOLUTIONARY TRADING ALGO  //  venues.tradovate
===================================
Tradovate futures adapter. OAuth2 + contract-month resolution + OSO bracket.
Real aiohttp HTTP wired. Creds-less constructor still returns mocks (safe
default for dry-run and unit tests).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from eta_engine.venues.base import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    VenueBase,
)

logger = logging.getLogger(__name__)

TRADOVATE_LIVE = "https://live.tradovateapi.com/v1"
TRADOVATE_DEMO = "https://demo.tradovateapi.com/v1"

_QUARTERLY_MONTHS: dict[int, str] = {3: "H", 6: "M", 9: "U", 12: "Z"}
_TOKEN_REFRESH_BUFFER_S = 300
_ROLL_BUSINESS_DAYS = 5
_HTTP_TIMEOUT_S = 10.0
_HTTP_RETRY = 1  # one retry on transient network error

_SIDE_MAP = {Side.BUY: "Buy", Side.SELL: "Sell"}
_OTYPE_MAP = {OrderType.MARKET: "Market", OrderType.LIMIT: "Limit", OrderType.POST_ONLY: "Limit"}


def _coerce_account_id(value: int | str | None) -> int:
    """Tradovate requires an integer accountId; missing/non-numeric stays safe at 0."""
    if value is None:
        return 0
    try:
        return int(str(value).strip() or "0")
    except ValueError:
        logger.warning("tradovate account_id is non-numeric; using accountId=0")
        return 0


def _third_friday(year: int, month: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=UTC)
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def _business_days_between(a: datetime, b: datetime) -> int:
    if b <= a:
        return 0
    days, cur = 0, a
    while cur.date() < b.date():
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


class TradovateVenue(VenueBase):
    """Tradovate futures execution surface."""

    name: str = "tradovate"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        *,
        demo: bool = True,
        app_id: str = "EtaEngine",
        app_version: str = "1.0",
        cid: str = "",
        app_secret: str = "",
        account_id: int | str | None = None,
    ) -> None:
        super().__init__(api_key, api_secret)
        self.demo: bool = demo
        self.app_id, self.app_version, self.cid = app_id, app_version, cid
        self.account_id: int = _coerce_account_id(
            account_id if account_id is not None else os.environ.get("TRADOVATE_ACCOUNT_ID"),
        )
        # Tradovate's OAuth2 /auth/accessTokenRequest wants TWO secrets:
        # `password` = user's Tradovate account password (api_secret here),
        # `sec`      = API-app secret issued when the app's cid was registered.
        # If the caller doesn't provide a separate app_secret, fall back to
        # api_secret for backward-compat with existing tests/stubs.
        self.app_secret: str = app_secret or api_secret
        self._access_token: str | None = None
        self._md_access_token: str | None = None
        self._expiration: datetime | None = None
        self._session: Any = None  # aiohttp.ClientSession, lazy

    def _base(self) -> str:
        return TRADOVATE_DEMO if self.demo else TRADOVATE_LIVE

    def _token_expiring(self) -> bool:
        if self._access_token is None or self._expiration is None:
            return True
        return (self._expiration - datetime.now(UTC)).total_seconds() < _TOKEN_REFRESH_BUFFER_S

    def _has_creds(self) -> bool:
        return bool(self.api_key) and bool(self.api_secret) and bool(self.app_id) and bool(self.cid)

    def has_credentials(self) -> bool:
        return bool(self.api_key) and bool(self.api_secret) and bool(self.app_id) and bool(self.cid)

    def connection_endpoint(self) -> str:
        return self._base()

    # ------------------------------------------------------------------ #
    # HTTP plumbing (designed to be easy to monkeypatch in tests)
    # ------------------------------------------------------------------ #
    async def _ensure_session(self) -> Any:  # noqa: ANN401 - aiohttp imported lazily; real type is aiohttp.ClientSession
        if self._session is None:
            import aiohttp  # noqa: PLC0415 - lazy import keeps module importable without aiohttp

            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S),
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session. Call on shutdown."""
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("tradovate.close: session close raised %s", e)
            self._session = None

    async def _http_post(self, path: str, body: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        """POST JSON body → (status, decoded_json). Single retry on transient ClientError."""
        url = f"{self._base()}{path}"
        session = await self._ensure_session()
        last_exc: Exception | None = None
        for attempt in range(_HTTP_RETRY + 1):
            try:
                async with session.post(url, data=body, headers=headers) as resp:
                    txt = await resp.text()
                    try:
                        data = json.loads(txt) if txt else {}
                    except json.JSONDecodeError:
                        data = {"_raw": txt}
                    return resp.status, data
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("tradovate POST %s attempt=%d failed: %s", path, attempt + 1, exc)
                if attempt >= _HTTP_RETRY:
                    break
        assert last_exc is not None
        raise last_exc

    async def _http_get(self, path: str, headers: dict[str, str]) -> tuple[int, Any]:
        """GET → (status, decoded_json). Single retry on transient ClientError."""
        url = f"{self._base()}{path}"
        session = await self._ensure_session()
        last_exc: Exception | None = None
        for attempt in range(_HTTP_RETRY + 1):
            try:
                async with session.get(url, headers=headers) as resp:
                    txt = await resp.text()
                    try:
                        data = json.loads(txt) if txt else {}
                    except json.JSONDecodeError:
                        data = {"_raw": txt}
                    return resp.status, data
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("tradovate GET %s attempt=%d failed: %s", path, attempt + 1, exc)
                if attempt >= _HTTP_RETRY:
                    break
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    async def authenticate(self) -> None:
        """POST /auth/accessTokenRequest -- sets access_token + md_access_token."""
        payload = {
            "name": self.api_key,
            "password": self.api_secret,
            "appId": self.app_id,
            "appVersion": self.app_version,
            "cid": self.cid,
            "sec": self.app_secret,
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = {"Content-Type": "application/json"}

        if not self._has_creds():
            # Safe stub path — no HTTP, no creds to send.
            self._access_token = "stub-access-token"
            self._md_access_token = "stub-md-token"
            self._expiration = datetime.now(UTC) + timedelta(minutes=60)
            logger.info("tradovate authenticate stub (no creds); expiry=%s", self._expiration)
            return

        status, data = await self._http_post("/auth/accessTokenRequest", body, headers)
        if status != 200 or "accessToken" not in data:
            logger.error("tradovate auth failed status=%s body=%s", status, data)
            err = data.get("errorText") or data.get("p-ticket")
            raise RuntimeError(
                f"tradovate authenticate failed: status={status} errorText={err}",
            )
        self._access_token = data["accessToken"]
        self._md_access_token = data.get("mdAccessToken") or data["accessToken"]
        exp_iso = data.get("expirationTime")
        if exp_iso:
            try:
                self._expiration = datetime.fromisoformat(exp_iso.replace("Z", "+00:00"))
            except ValueError:
                self._expiration = datetime.now(UTC) + timedelta(minutes=60)
        else:
            self._expiration = datetime.now(UTC) + timedelta(minutes=60)
        logger.info("tradovate authenticated; token valid until %s", self._expiration)

    async def _ensure_token(self) -> None:
        if self._token_expiring():
            await self.authenticate()

    # ------------------------------------------------------------------ #
    # Contract resolution
    # ------------------------------------------------------------------ #
    def resolve_contract(self, root: str = "MNQ", month: str = "front", ref: datetime | None = None) -> str:
        """Resolve root → front-month symbol, e.g. 'MNQM6'. Rolls N bdays before expiry."""
        now = ref or datetime.now(UTC)
        quarterly = sorted(_QUARTERLY_MONTHS)
        for year_off in (0, 1):
            for m in quarterly:
                year = now.year + year_off
                if year_off == 0 and m < now.month:
                    continue
                expiry = _third_friday(year, m)
                bdays = _business_days_between(now, expiry)
                if year_off == 0 and m == now.month and bdays <= _ROLL_BUSINESS_DAYS:
                    continue
                _ = month  # 'front' default; 'back' reserved
                return f"{root}{_QUARTERLY_MONTHS[m]}{year % 10}"
        m = quarterly[0]
        return f"{root}{_QUARTERLY_MONTHS[m]}{(now.year + 1) % 10}"

    def _build_place_payload(self, request: OrderRequest, account_id: int | None = None) -> dict[str, Any]:
        symbol = request.symbol
        if symbol.upper() in {"MNQ", "NQ", "ES", "MES", "RTY"}:
            symbol = self.resolve_contract(symbol)
        resolved_account_id = self.account_id if account_id is None else account_id
        payload: dict[str, Any] = {
            "accountId": resolved_account_id,
            "action": _SIDE_MAP[request.side],
            "symbol": symbol,
            "orderQty": int(request.qty),
            "orderType": _OTYPE_MAP[request.order_type],
            "isAutomated": True,
            "clOrdId": self.idempotency_key(request),
        }
        if request.order_type is OrderType.LIMIT and request.price is not None:
            payload["price"] = request.price
        return payload

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    async def place_order(self, request: OrderRequest) -> OrderResult:
        await self._ensure_token()
        payload = self._build_place_payload(request)
        body = json.dumps(payload, separators=(",", ":"))
        headers = {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"}
        logger.info("tradovate.place_order %s %s qty=%s", payload["symbol"], payload["action"], payload["orderQty"])

        if not self._has_creds():
            mock_raw = {"orderId": 100_000_000 + hash(payload["clOrdId"]) % 1_000_000, "clOrdId": payload["clOrdId"]}
            return OrderResult(order_id=str(mock_raw["orderId"]), status=OrderStatus.OPEN, raw=mock_raw)

        status, data = await self._http_post("/order/placeOrder", body, headers)
        if status != 200 or "orderId" not in data:
            logger.error("tradovate.place_order rejected status=%s body=%s", status, data)
            return OrderResult(
                order_id=str(data.get("orderId") or payload["clOrdId"]),
                status=OrderStatus.REJECTED,
                raw={"http_status": status, **data},
            )
        return OrderResult(
            order_id=str(data["orderId"]),
            status=OrderStatus.OPEN,
            raw=data,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        await self._ensure_token()
        payload = {"orderId": int(order_id) if order_id.isdigit() else 0}
        body = json.dumps(payload, separators=(",", ":"))
        headers = {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"}
        logger.info("tradovate.cancel_order %s", order_id)

        if not self._has_creds():
            _ = symbol
            return True

        status, data = await self._http_post("/order/cancelOrder", body, headers)
        if status != 200:
            logger.error("tradovate.cancel_order failed status=%s body=%s", status, data)
            return False
        return bool(data.get("ok", True))

    async def get_positions(self) -> list[dict[str, Any]]:
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if not self._has_creds():
            return []
        status, data = await self._http_get("/position/list", headers)
        if status != 200 or not isinstance(data, list):
            logger.warning("tradovate.get_positions status=%s body-type=%s", status, type(data).__name__)
            return []
        return data

    async def get_balance(self) -> dict[str, float]:
        await self._ensure_token()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if not self._has_creds():
            return {"USD": 0.0}
        status, data = await self._http_get("/cashBalance/list", headers)
        if status != 200 or not isinstance(data, list) or not data:
            logger.warning("tradovate.get_balance status=%s", status)
            return {"USD": 0.0}
        # cashBalance/list returns list of account balances — sum the amount field
        total = 0.0
        for row in data:
            try:
                total += float(row.get("amount") or 0.0)
            except (TypeError, ValueError):
                continue
        return {"USD": total}

    async def bracket_order(
        self,
        entry: OrderRequest,
        stop_price: float,
        target_price: float,
    ) -> list[OrderResult]:
        """OSO: entry + OCO(stop, target). Returns [entry, stop, target]."""
        await self._ensure_token()
        entry_payload = self._build_place_payload(entry)
        exit_action = "Sell" if entry.side is Side.BUY else "Buy"
        bracket_payload: dict[str, Any] = {
            "entry": entry_payload,
            "brackets": [
                {"action": exit_action, "orderType": "Stop", "stopPrice": stop_price},
                {"action": exit_action, "orderType": "Limit", "price": target_price},
            ],
        }
        body = json.dumps(bracket_payload, separators=(",", ":"))
        headers = {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"}
        parent_id = f"oso-{self.idempotency_key(entry)[:12]}"
        logger.info("tradovate.bracket parent=%s stop=%s target=%s", parent_id, stop_price, target_price)

        if not self._has_creds():
            return [
                OrderResult(order_id=parent_id, status=OrderStatus.OPEN, raw={"leg": "entry", "parent": parent_id}),
                OrderResult(
                    order_id=f"{parent_id}-S",
                    status=OrderStatus.OPEN,
                    avg_price=stop_price,
                    raw={"leg": "stop", "parent": parent_id},
                ),
                OrderResult(
                    order_id=f"{parent_id}-T",
                    status=OrderStatus.OPEN,
                    avg_price=target_price,
                    raw={"leg": "target", "parent": parent_id},
                ),
            ]

        status, data = await self._http_post("/order/placeOSO", body, headers)
        if status != 200 or "orderId" not in data:
            logger.error("tradovate.bracket rejected status=%s body=%s", status, data)
            return [
                OrderResult(
                    order_id=parent_id,
                    status=OrderStatus.REJECTED,
                    raw={"http_status": status, **(data if isinstance(data, dict) else {})},
                ),
            ]
        parent_real = str(data["orderId"])
        return [
            OrderResult(
                order_id=parent_real, status=OrderStatus.OPEN, raw={"leg": "entry", "parent": parent_real, **data}
            ),
            OrderResult(
                order_id=f"{parent_real}-S",
                status=OrderStatus.OPEN,
                avg_price=stop_price,
                raw={"leg": "stop", "parent": parent_real},
            ),
            OrderResult(
                order_id=f"{parent_real}-T",
                status=OrderStatus.OPEN,
                avg_price=target_price,
                raw={"leg": "target", "parent": parent_real},
            ),
        ]
