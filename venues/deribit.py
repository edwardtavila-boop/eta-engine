"""EVOLUTIONARY TRADING ALGO  //  venues.deribit.

Deribit options venue adapter (read-first; order placement gated).

Why this module exists
----------------------
The tail-hedge engine in `core.tail_hedge` prices OTM SPY puts against
an equity-heavy book. For crypto-heavy sessions (ETH/SOL/XRP bots on
perps) a SPY put is mispriced -- crypto tail risk comes from BTC/ETH
implied-vol moves, not equity index moves. Deribit is the canonical
venue for BTC + ETH options; exposing it as a first-class read surface
lets the tail engine price crypto-native puts.

Scope
-----
* **Read-only by default.** ``has_credentials`` defaults False; orders
  are rejected with a ``NotImplementedError`` until explicit sign-off.
  Keeps Deribit safely out of the live order path.
* **Option-chain facade.** ``fetch_option_chain`` returns a structured
  chain for a given underlying + expiry, sorted by strike.
* **IV surface snapshot.** ``fetch_iv`` returns at-the-money implied
  volatility for a given expiry -- the single input the tail-hedge
  pricer needs.
* **MCP-first.** The module accepts an optional ``mcp_client``
  (``mcp__deribit__*`` shape) and falls back to the public REST API at
  ``www.deribit.com/api/v2``. Both paths produce the same schema.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from eta_engine.venues.base import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    VenueBase,
)

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.deribit.com/api/v2"


__all__ = [
    "DeribitClient",
    "OptionContract",
    "OptionChain",
    "DeribitMcp",
    "atm_iv_to_tail_sigma",
]


@dataclass(frozen=True)
class OptionContract:
    """One Deribit option instrument snapshot."""

    instrument_name: str  # e.g. "BTC-28JUN24-60000-P"
    underlying: str  # "BTC" / "ETH"
    strike: float
    expiry_ts_ms: int
    is_put: bool
    mark_price: float
    mark_iv: float
    bid: float
    ask: float


@dataclass(frozen=True)
class OptionChain:
    """Filtered option chain for one (underlying, expiry) pair."""

    underlying: str
    expiry_ts_ms: int
    puts: tuple[OptionContract, ...]
    calls: tuple[OptionContract, ...]

    def put_at_strike(self, strike: float) -> OptionContract | None:
        """Return the first put whose strike is <= `strike`, favoring the deepest ITM."""
        cands = [c for c in self.puts if c.strike <= strike]
        if not cands:
            return None
        return max(cands, key=lambda c: c.strike)


class DeribitMcp(Protocol):
    """Minimal shape of an `mcp__deribit__*` wrapper."""

    def get_instruments(self, *, currency: str, kind: str, expired: bool = False) -> list[dict[str, Any]]: ...

    def get_tickers(self, *, instrument_names: list[str]) -> list[dict[str, Any]]: ...

    def get_index_price(self, *, index_name: str) -> dict[str, Any]: ...


class DeribitClient(VenueBase):
    """Read-only Deribit facade. Orders are blocked until enabled."""

    name: str = "deribit"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        *,
        base_url: str | None = None,
        timeout_s: float = 10.0,
        mcp_client: DeribitMcp | None = None,
        allow_orders: bool = False,
    ) -> None:
        super().__init__(api_key=api_key, api_secret=api_secret)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_s = max(1.0, float(timeout_s))
        self._mcp = mcp_client
        self._allow_orders = bool(allow_orders)

    def connection_endpoint(self) -> str | None:
        return self.base_url

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------

    def fetch_option_chain(self, *, underlying: str, expiry_ts_ms: int) -> OptionChain:
        """Return the option chain for one expiry (filtered from full instrument list)."""
        instruments = self._fetch_instruments(underlying=underlying)
        tickers = self._fetch_tickers([i["instrument_name"] for i in instruments if _expiry_of(i) == expiry_ts_ms])
        ticker_map = {t["instrument_name"]: t for t in tickers}

        puts: list[OptionContract] = []
        calls: list[OptionContract] = []
        for inst in instruments:
            if _expiry_of(inst) != expiry_ts_ms:
                continue
            name = inst["instrument_name"]
            ticker = ticker_map.get(name, {})
            strike = _float(inst.get("strike"))
            mark_px = _float(ticker.get("mark_price"))
            mark_iv = _float(ticker.get("mark_iv"))
            bid = _float(ticker.get("best_bid_price"))
            ask = _float(ticker.get("best_ask_price"))
            contract = OptionContract(
                instrument_name=name,
                underlying=underlying.upper(),
                strike=strike,
                expiry_ts_ms=expiry_ts_ms,
                is_put=inst.get("option_type") == "put",
                mark_price=mark_px,
                mark_iv=mark_iv,
                bid=bid,
                ask=ask,
            )
            (puts if contract.is_put else calls).append(contract)
        puts.sort(key=lambda c: c.strike, reverse=True)
        calls.sort(key=lambda c: c.strike)
        return OptionChain(
            underlying=underlying.upper(),
            expiry_ts_ms=expiry_ts_ms,
            puts=tuple(puts),
            calls=tuple(calls),
        )

    def fetch_atm_iv(
        self,
        *,
        underlying: str,
        expiry_ts_ms: int,
        spot_override: float | None = None,
    ) -> float:
        """Return ATM implied volatility as a decimal (e.g. 0.65 for 65%).

        Deribit quotes ``mark_iv`` in percent; we normalize to decimal.
        Returns 0.0 when no data is available (safe fallback for the
        tail-hedge pricer, which will then skip the Deribit leg).
        """
        chain = self.fetch_option_chain(underlying=underlying, expiry_ts_ms=expiry_ts_ms)
        spot = spot_override if spot_override is not None else self._fetch_index_price(underlying)
        if spot <= 0.0:
            return 0.0
        best = min(
            (c for c in (*chain.puts, *chain.calls) if c.mark_iv > 0.0),
            key=lambda c: abs(c.strike - spot),
            default=None,
        )
        if best is None:
            return 0.0
        # Deribit reports IV as a percentage
        return best.mark_iv / 100.0 if best.mark_iv > 1.5 else best.mark_iv

    # ------------------------------------------------------------------
    # VenueBase contract
    # ------------------------------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderResult:
        if not self._allow_orders:
            return OrderResult(
                order_id="",
                status=OrderStatus.REJECTED,
                raw={
                    "reason": "deribit orders disabled (allow_orders=False)",
                    "request": request.model_dump(),
                },
            )
        # Intentionally not implemented; guarded by allow_orders to keep
        # the venue safely read-only until the operator explicitly opts in.
        raise NotImplementedError(
            "DeribitClient.place_order not yet wired. Route options execution through the Firm board before enabling."
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        return False

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def get_balance(self) -> dict[str, float]:
        return {}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_instruments(self, *, underlying: str) -> list[dict[str, Any]]:
        if self._mcp is not None:
            try:
                return self._mcp.get_instruments(currency=underlying.upper(), kind="option") or []
            except Exception as exc:  # noqa: BLE001
                log.debug("deribit MCP instruments failed: %s", exc)
        url = (
            f"{self.base_url}/public/get_instruments?"
            f"{urllib.parse.urlencode({'currency': underlying.upper(), 'kind': 'option', 'expired': 'false'})}"
        )
        payload = _json_get(url, timeout_s=self.timeout_s)
        return payload.get("result") or []

    def _fetch_tickers(self, instrument_names: list[str]) -> list[dict[str, Any]]:
        if not instrument_names:
            return []
        if self._mcp is not None:
            try:
                return self._mcp.get_tickers(instrument_names=instrument_names) or []
            except Exception as exc:  # noqa: BLE001
                log.debug("deribit MCP tickers failed: %s", exc)
        out: list[dict[str, Any]] = []
        # Public API is per-instrument; batch here is best-effort.
        for name in instrument_names[:50]:  # cap to avoid blowing up free tier
            url = f"{self.base_url}/public/ticker?{urllib.parse.urlencode({'instrument_name': name})}"
            try:
                payload = _json_get(url, timeout_s=self.timeout_s)
            except Exception as exc:  # noqa: BLE001
                log.debug("deribit ticker %s failed: %s", name, exc)
                continue
            result = payload.get("result") or {}
            result.setdefault("instrument_name", name)
            out.append(result)
        return out

    def _fetch_index_price(self, underlying: str) -> float:
        index_name = f"{underlying.lower()}_usd"
        if self._mcp is not None:
            try:
                resp = self._mcp.get_index_price(index_name=index_name) or {}
                return _float(resp.get("index_price") or resp.get("price"))
            except Exception as exc:  # noqa: BLE001
                log.debug("deribit MCP index_price failed: %s", exc)
        url = f"{self.base_url}/public/get_index_price?{urllib.parse.urlencode({'index_name': index_name})}"
        try:
            payload = _json_get(url, timeout_s=self.timeout_s)
        except Exception as exc:  # noqa: BLE001
            log.debug("deribit index_price REST failed: %s", exc)
            return 0.0
        result = payload.get("result") or {}
        return _float(result.get("index_price") or result.get("price"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def atm_iv_to_tail_sigma(atm_iv_annualized: float, days_to_expiry: int) -> float:
    """Convert annualized ATM IV to a per-period sigma for the tail-hedge pricer.

    `core.tail_hedge._bs_put_price` takes annualized sigma, so the return
    is just `atm_iv_annualized` clamped to a sane range. Included as a
    named helper to make the tail-hedge integration explicit.
    """
    _ = days_to_expiry  # reserved for future per-period adjustments
    return max(0.0, min(3.0, float(atm_iv_annualized)))


def _json_get(url: str, *, timeout_s: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "eta-engine/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _float(raw: object, default: float = 0.0) -> float:
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _expiry_of(instrument: dict[str, Any]) -> int:
    raw = instrument.get("expiration_timestamp") or instrument.get("expiry_timestamp") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _now_ms() -> int:
    return int(time.time() * 1000)
