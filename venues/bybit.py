"""
EVOLUTIONARY TRADING ALGO  //  venues.bybit
===============================
Bybit Unified v5 linear-perp adapter. Isolated margin default.
Real aiohttp HTTP wired. Signing, request building, parsing, error mapping,
rate-limit tracking live. Creds-less constructor still returns mocks.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import deque
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

BYBIT_V5_HOST = "https://api.bybit.com"
BYBIT_V5_TESTNET = "https://api-testnet.bybit.com"

_RECV_WINDOW = "5000"
_RATE_LIMIT_WINDOW_S = 1.0
_RATE_LIMIT_MAX = 10
_HTTP_TIMEOUT_S = 10.0
_HTTP_RETRY = 1

_SIDE_MAP = {Side.BUY: "Buy", Side.SELL: "Sell"}
_OTYPE_MAP = {OrderType.MARKET: "Market", OrderType.LIMIT: "Limit", OrderType.POST_ONLY: "Limit"}


class BybitVenue(VenueBase):
    """Bybit v5 unified-account (linear perps)."""

    name: str = "bybit"

    SYMBOL_MAPPING: dict[str, str] = {
        "BTC/USDT:USDT": "BTCUSDT",
        "ETH/USDT:USDT": "ETHUSDT",
        "SOL/USDT:USDT": "SOLUSDT",
        "XRP/USDT:USDT": "XRPUSDT",
    }

    def __init__(self, api_key: str = "", api_secret: str = "", *, testnet: bool = False) -> None:
        super().__init__(api_key, api_secret)
        self.testnet: bool = testnet
        self._last_request_times: deque[float] = deque(maxlen=_RATE_LIMIT_MAX * 4)
        self._session: Any = None  # aiohttp.ClientSession, lazy

    def _host(self) -> str:
        return BYBIT_V5_TESTNET if self.testnet else BYBIT_V5_HOST

    def _has_creds(self) -> bool:
        return bool(self.api_key) and bool(self.api_secret)

    def _native_symbol(self, symbol: str) -> str:
        if symbol in self.SYMBOL_MAPPING:
            return self.SYMBOL_MAPPING[symbol]
        if "/" in symbol:
            base, _, rest = symbol.partition("/")
            return f"{base}{rest.split(':', 1)[0]}"
        return symbol

    def _sign(self, timestamp: str, recv_window: str, payload: str) -> str:
        """HMAC-SHA256 signature per Bybit v5 spec."""
        prehash = timestamp + self.api_key + recv_window + payload
        return hmac.new(self.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).hexdigest()

    def _headers(self, payload: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "X-BAPI-SIGN": self._sign(ts, _RECV_WINDOW, payload),
            "Content-Type": "application/json",
        }

    def _mark_request(self) -> None:
        now = time.monotonic()
        self._last_request_times.append(now)
        cutoff = now - _RATE_LIMIT_WINDOW_S
        while self._last_request_times and self._last_request_times[0] < cutoff:
            self._last_request_times.popleft()
        if len(self._last_request_times) > _RATE_LIMIT_MAX:
            logger.warning("bybit rate-limit pressure: %d req/s", len(self._last_request_times))

    def _parse_order_response(self, raw: dict[str, Any], fallback_id: str) -> OrderResult:
        code = raw.get("retCode", -1)
        result = raw.get("result") or {}
        if code != 0:
            logger.error("bybit rejected retCode=%s retMsg=%s", code, raw.get("retMsg"))
            return OrderResult(
                order_id=str(result.get("orderLinkId") or fallback_id),
                status=OrderStatus.REJECTED, raw=raw,
            )
        return OrderResult(
            order_id=str(result.get("orderId") or result.get("orderLinkId") or fallback_id),
            status=OrderStatus.OPEN, raw=raw,
        )

    def _build_place_payload(self, request: OrderRequest) -> dict[str, Any]:
        native = self._native_symbol(request.symbol)
        if request.order_type is OrderType.POST_ONLY:
            tif = "PostOnly"
        elif request.order_type is OrderType.MARKET:
            tif = "IOC"
        else:
            tif = "GTC"
        payload: dict[str, Any] = {
            "category": "linear",
            "symbol": native,
            "side": _SIDE_MAP[request.side],
            "orderType": _OTYPE_MAP[request.order_type],
            "qty": f"{request.qty}",
            "timeInForce": tif,
            "reduceOnly": request.reduce_only,
            "orderLinkId": self.idempotency_key(request),
        }
        if request.price is not None and request.order_type is not OrderType.MARKET:
            payload["price"] = f"{request.price}"
        return payload

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    async def _ensure_session(self) -> Any:  # noqa: ANN401 - aiohttp imported lazily; real type is aiohttp.ClientSession
        if self._session is None:
            import aiohttp  # noqa: PLC0415
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("bybit.close session close raised %s", e)
            self._session = None

    async def _http_post(self, path: str, body: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        url = f"{self._host()}{path}"
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
                logger.warning("bybit POST %s attempt=%d failed: %s", path, attempt + 1, exc)
                if attempt >= _HTTP_RETRY:
                    break
        assert last_exc is not None
        raise last_exc

    async def _http_get(self, path: str, qs: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        url = f"{self._host()}{path}?{qs}" if qs else f"{self._host()}{path}"
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
                logger.warning("bybit GET %s attempt=%d failed: %s", path, attempt + 1, exc)
                if attempt >= _HTTP_RETRY:
                    break
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def place_order(self, request: OrderRequest) -> OrderResult:
        self._mark_request()
        payload = self._build_place_payload(request)
        body = json.dumps(payload, separators=(",", ":"))
        headers = self._headers(body)
        logger.info("bybit.place_order %s %s qty=%s", payload["symbol"], payload["side"], payload["qty"])

        if not self._has_creds():
            mock_raw = {
                "retCode": 0,
                "result": {"orderId": f"mock-{int(time.time() * 1000)}", "orderLinkId": payload["orderLinkId"]},
                "retExtInfo": {}, "time": int(time.time() * 1000),
            }
            return self._parse_order_response(mock_raw, fallback_id=payload["orderLinkId"])

        status, data = await self._http_post("/v5/order/create", body, headers)
        if status != 200:
            logger.error("bybit.place_order http=%s body=%s", status, data)
            return OrderResult(
                order_id=payload["orderLinkId"], status=OrderStatus.REJECTED,
                raw={"http_status": status, **(data if isinstance(data, dict) else {})},
            )
        return self._parse_order_response(data, fallback_id=payload["orderLinkId"])

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        self._mark_request()
        payload = {"category": "linear", "symbol": self._native_symbol(symbol), "orderId": order_id}
        body = json.dumps(payload, separators=(",", ":"))
        headers = self._headers(body)
        logger.info("bybit.cancel_order %s %s", payload["symbol"], order_id)

        if not self._has_creds():
            return True

        status, data = await self._http_post("/v5/order/cancel", body, headers)
        if status != 200:
            logger.error("bybit.cancel_order http=%s body=%s", status, data)
            return False
        return int(data.get("retCode", -1)) == 0

    async def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self._mark_request()
        qs = f"category=linear&symbol={self._native_symbol(symbol)}" if symbol else "category=linear&settleCoin=USDT"
        headers = self._headers(qs)

        if not self._has_creds():
            return []

        status, data = await self._http_get("/v5/position/list", qs, headers)
        if status != 200 or int(data.get("retCode", -1)) != 0:
            logger.warning("bybit.get_positions http=%s retCode=%s", status, data.get("retCode"))
            return []
        result = data.get("result") or {}
        return list(result.get("list") or [])

    async def get_balance(self, coin: str = "USDT") -> dict[str, float]:
        self._mark_request()
        qs = f"accountType=UNIFIED&coin={coin}"
        headers = self._headers(qs)

        if not self._has_creds():
            return {coin: 0.0}

        status, data = await self._http_get("/v5/account/wallet-balance", qs, headers)
        if status != 200 or int(data.get("retCode", -1)) != 0:
            logger.warning("bybit.get_balance http=%s retCode=%s", status, data.get("retCode"))
            return {coin: 0.0}
        # Shape: result.list[].coin[].walletBalance
        result = data.get("result") or {}
        accounts = result.get("list") or []
        total = 0.0
        for acct in accounts:
            for c in acct.get("coin") or []:
                if c.get("coin") == coin:
                    try:
                        total += float(c.get("walletBalance") or 0.0)
                    except (TypeError, ValueError):
                        continue
        return {coin: total}

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        self._mark_request()
        payload = {
            "category": "linear", "symbol": self._native_symbol(symbol),
            "buyLeverage": str(leverage), "sellLeverage": str(leverage),
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = self._headers(body)
        logger.info("bybit.set_leverage %s x%s", payload["symbol"], leverage)

        if not self._has_creds():
            return True

        status, data = await self._http_post("/v5/position/set-leverage", body, headers)
        if status != 200:
            logger.error("bybit.set_leverage http=%s body=%s", status, data)
            return False
        return int(data.get("retCode", -1)) == 0

    async def set_isolated_margin(self, symbol: str) -> bool:
        self._mark_request()
        payload = {
            "category": "linear", "symbol": self._native_symbol(symbol),
            "tradeMode": 1, "buyLeverage": "10", "sellLeverage": "10",
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = self._headers(body)
        logger.info("bybit.set_isolated_margin %s", payload["symbol"])

        if not self._has_creds():
            return True

        status, data = await self._http_post("/v5/position/switch-isolated", body, headers)
        if status != 200:
            logger.error("bybit.set_isolated_margin http=%s body=%s", status, data)
            return False
        # retCode 110026 = "already isolated" - treat as success
        rc = int(data.get("retCode", -1))
        return rc == 0 or rc == 110026
