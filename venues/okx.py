"""
EVOLUTIONARY TRADING ALGO  //  venues.okx
=============================
OKX V5 — backup to Bybit for crypto perps.

HMAC signing per OKX v5 spec: Base64(HMAC-SHA256(timestamp + method + requestPath + body)).
Creds-less constructor still returns mocks (safe for dry-run / unit tests).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
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

_HTTP_TIMEOUT_S = 10.0
_HTTP_RETRY = 1

_SIDE_MAP = {Side.BUY: "buy", Side.SELL: "sell"}
_OTYPE_MAP = {OrderType.MARKET: "market", OrderType.LIMIT: "limit", OrderType.POST_ONLY: "post_only"}


class OkxVenue(VenueBase):
    """OKX V5 unified account."""

    name: str = "okx"

    REST_BASE = "https://www.okx.com"
    PATH_PLACE = "/api/v5/trade/order"
    PATH_CANCEL = "/api/v5/trade/cancel-order"
    PATH_POSITIONS = "/api/v5/account/positions"
    PATH_BALANCE = "/api/v5/account/balance"

    SYMBOL_MAPPING: dict[str, str] = {
        "BTC/USDT:USDT": "BTC-USDT-SWAP",
        "ETH/USDT:USDT": "ETH-USDT-SWAP",
        "SOL/USDT:USDT": "SOL-USDT-SWAP",
        "XRP/USDT:USDT": "XRP-USDT-SWAP",
    }

    def __init__(self, api_key: str = "", api_secret: str = "", passphrase: str = "") -> None:
        super().__init__(api_key, api_secret)
        self.passphrase = passphrase
        self._session: Any = None

    def _has_creds(self) -> bool:
        return bool(self.api_key) and bool(self.api_secret) and bool(self.passphrase)

    def has_credentials(self) -> bool:
        return self._has_creds()

    def connection_endpoint(self) -> str:
        return self.REST_BASE

    def _native_symbol(self, symbol: str) -> str:
        if symbol in self.SYMBOL_MAPPING:
            return self.SYMBOL_MAPPING[symbol]
        if "/" in symbol:
            # Generic "X/Y:Y" -> X-Y-SWAP
            base, _, rest = symbol.partition("/")
            quote = rest.split(":", 1)[0]
            return f"{base}-{quote}-SWAP"
        return symbol

    # ------------------------------------------------------------------ #
    # Signing
    # ------------------------------------------------------------------ #
    def _timestamp(self) -> str:
        # ISO8601 with millisecond precision, UTC, trailing 'Z'
        now = datetime.now(UTC)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _sign(self, ts: str, method: str, request_path: str, body: str) -> str:
        prehash = f"{ts}{method.upper()}{request_path}{body}"
        mac = hmac.new(self.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        ts = self._timestamp()
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

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
                logger.warning("okx.close session close raised %s", e)
            self._session = None

    async def _http_post(self, request_path: str, body: str) -> tuple[int, dict[str, Any]]:
        url = f"{self.REST_BASE}{request_path}"
        headers = self._headers("POST", request_path, body)
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
                logger.warning("okx POST %s attempt=%d failed: %s", request_path, attempt + 1, exc)
                if attempt >= _HTTP_RETRY:
                    break
        assert last_exc is not None
        raise last_exc

    async def _http_get(
        self,
        request_path: str,
        qs: str = "",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        full_path = f"{request_path}?{qs}" if qs else request_path
        url = f"{self.REST_BASE}{full_path}"
        # OKX signing uses the full path including query string. Public endpoints
        # can explicitly pass empty headers to avoid sending auth material.
        headers = headers if headers is not None else self._headers("GET", full_path, "")
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
                logger.warning("okx GET %s attempt=%d failed: %s", request_path, attempt + 1, exc)
                if attempt >= _HTTP_RETRY:
                    break
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _coerce_book_levels(
        raw_levels: Any,  # noqa: ANN401 -- exchange book payloads are untyped lists
    ) -> list[tuple[float, float]]:
        levels: list[tuple[float, float]] = []
        if not isinstance(raw_levels, list):
            return levels
        for entry in raw_levels:
            price: Any
            size: Any
            if isinstance(entry, dict):
                price = entry.get("px") or entry.get("price") or entry.get("p")
                size = entry.get("sz") or entry.get("size") or entry.get("s") or entry.get("qty")
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, size = entry[0], entry[1]
            else:
                continue
            try:
                price_f = float(price)
                size_f = float(size)
            except (TypeError, ValueError):
                continue
            if price_f > 0.0 and size_f >= 0.0:
                levels.append((price_f, size_f))
        return levels

    @staticmethod
    def _book_regime(spread_bps: float, book_imbalance: float) -> str:
        abs_imb = abs(book_imbalance)
        if spread_bps <= 0.0 and abs_imb <= 0.0:
            return "UNKNOWN"
        if spread_bps <= 1.5 and abs_imb <= 0.20:
            return "TIGHT"
        if spread_bps <= 4.5 and abs_imb <= 0.40:
            return "NORMAL"
        if spread_bps <= 12.0 and abs_imb <= 0.65:
            return "WIDE"
        return "STRESSED"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def _build_place_payload(self, request: OrderRequest) -> dict[str, Any]:
        native = self._native_symbol(request.symbol)
        payload: dict[str, Any] = {
            "instId": native,
            "tdMode": "isolated",
            "side": _SIDE_MAP[request.side],
            "ordType": _OTYPE_MAP[request.order_type],
            "sz": f"{request.qty}",
            "clOrdId": self.idempotency_key(request),
        }
        if request.order_type is not OrderType.MARKET and request.price is not None:
            payload["px"] = f"{request.price}"
        if request.reduce_only:
            payload["reduceOnly"] = True
        return payload

    async def place_order(self, request: OrderRequest) -> OrderResult:
        native = self._native_symbol(request.symbol)
        client_id = self.idempotency_key(request)
        logger.info("okx.place_order %s %s qty=%s id=%s", native, request.side.value, request.qty, client_id)

        if not self._has_creds():
            return OrderResult(
                order_id=client_id,
                status=OrderStatus.OPEN,
                filled_qty=0.0,
                avg_price=request.price or 0.0,
                raw={"stub": True, "instId": native},
            )

        payload = self._build_place_payload(request)
        body = json.dumps(payload, separators=(",", ":"))
        status, data = await self._http_post(self.PATH_PLACE, body)
        if status != 200 or str(data.get("code", "")) != "0":
            logger.error("okx.place_order http=%s code=%s msg=%s", status, data.get("code"), data.get("msg"))
            return OrderResult(
                order_id=client_id,
                status=OrderStatus.REJECTED,
                raw={"http_status": status, **(data if isinstance(data, dict) else {})},
            )
        # Success shape: { code: "0", data: [{ ordId, clOrdId, sCode, sMsg }] }
        inner = (data.get("data") or [{}])[0]
        order_id = str(inner.get("ordId") or client_id)
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.OPEN,
            raw=data,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        native = self._native_symbol(symbol)
        logger.info("okx.cancel_order %s %s", native, order_id)
        if not self._has_creds():
            return True
        payload = {"instId": native, "ordId": order_id}
        body = json.dumps(payload, separators=(",", ":"))
        status, data = await self._http_post(self.PATH_CANCEL, body)
        if status != 200:
            return False
        return str(data.get("code", "")) == "0"

    async def get_positions(self) -> list[dict[str, Any]]:
        if not self._has_creds():
            return []
        status, data = await self._http_get(self.PATH_POSITIONS)
        if status != 200 or str(data.get("code", "")) != "0":
            return []
        return list(data.get("data") or [])

    async def get_balance(self) -> dict[str, float]:
        if not self._has_creds():
            return {"USDT": 0.0}
        status, data = await self._http_get(self.PATH_BALANCE)
        if status != 200 or str(data.get("code", "")) != "0":
            return {"USDT": 0.0}
        accounts = data.get("data") or []
        total = 0.0
        for acct in accounts:
            for d in acct.get("details") or []:
                if d.get("ccy") == "USDT":
                    try:
                        total += float(d.get("availBal") or 0.0)
                    except (TypeError, ValueError):
                        continue
        return {"USDT": total}

    async def get_order_book(self, symbol: str, depth: int = 5) -> dict[str, Any] | None:
        native = self._native_symbol(symbol)
        limit = max(1, min(int(depth or 5), 400))
        status, data = await self._http_get("/api/v5/market/books", f"instId={native}&sz={limit}", {})
        if status != 200 or str(data.get("code", "")) != "0":
            logger.warning("okx.get_order_book http=%s code=%s", status, data.get("code"))
            return None
        rows = data.get("data") or []
        if not rows:
            return None
        row = rows[0] or {}
        bids = sorted(self._coerce_book_levels(row.get("bids") or []), key=lambda item: item[0], reverse=True)
        asks = sorted(self._coerce_book_levels(row.get("asks") or []), key=lambda item: item[0])
        if not bids or not asks:
            return None

        bid_price, bid_qty = bids[0]
        ask_price, ask_qty = asks[0]
        bid_depth = sum(qty for _, qty in bids[:limit])
        ask_depth = sum(qty for _, qty in asks[:limit])
        mid = (bid_price + ask_price) / 2.0
        spread = max(0.0, ask_price - bid_price)
        spread_bps = (spread / mid) * 10_000.0 if mid > 0.0 else 0.0
        depth_total = bid_depth + ask_depth
        book_imbalance = ((bid_depth - ask_depth) / depth_total) if depth_total > 0.0 else 0.0
        microprice = (
            (ask_price * bid_qty + bid_price * ask_qty) / (bid_qty + ask_qty) if (bid_qty + ask_qty) > 0.0 else mid
        )
        weighted_mid = (bid_price * ask_depth + ask_price * bid_depth) / depth_total if depth_total > 0.0 else mid
        ts_raw = row.get("ts") or data.get("ts")
        try:
            ts_ms = int(float(ts_raw))
        except (TypeError, ValueError):
            ts_ms = int(datetime.now(UTC).timestamp() * 1000)
        payload = {
            "venue": self.name,
            "order_book_venue": self.name,
            "symbol": native,
            "order_book_depth": limit,
            "ts": ts_ms,
            "bid_price": bid_price,
            "ask_price": ask_price,
            "best_bid": bid_price,
            "best_ask": ask_price,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "depth_1": bid_qty,
            "depth_5": ask_qty,
            "notional_depth_1": bid_price * bid_qty,
            "notional_depth_5": ask_price * ask_qty,
            "mid": mid,
            "weighted_mid": weighted_mid,
            "microprice": microprice,
            "spread": spread,
            "spread_bps": spread_bps,
            "book_imbalance": book_imbalance,
            "spread_regime": self._book_regime(spread_bps, book_imbalance),
            "raw": data,
        }
        return payload
