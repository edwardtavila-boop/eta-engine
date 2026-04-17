"""
EVOLUTIONARY TRADING ALGO  //  venues.router
================================
Smart routing with failover + circuit breakers.
Crypto (ETH/BTC/SOL/XRP USDT) → Bybit primary, OKX fallback.
Futures (MNQ/NQ/ES/MES/RTY) → Tradovate primary, IBKR fallback (stub).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from eta_engine.venues.base import OrderRequest, OrderResult, OrderStatus, VenueBase
from eta_engine.venues.bybit import BybitVenue
from eta_engine.venues.okx import OkxVenue
from eta_engine.venues.tradovate import TradovateVenue

logger = logging.getLogger(__name__)

Urgency = Literal["low", "normal", "high"]

_CRYPTO_NATIVES = {"ETHUSDT", "BTCUSDT", "SOLUSDT", "XRPUSDT"}
_FUTURES_ROOTS = ("MNQ", "NQ", "ES", "MES", "RTY")


def _is_futures(symbol: str) -> bool:
    return symbol.upper().startswith(_FUTURES_ROOTS)


def _is_crypto(symbol: str) -> bool:
    up = symbol.upper()
    if up in _CRYPTO_NATIVES:
        return True
    return "/" in up or "USDT" in up or "USDC" in up


@dataclass
class CircuitBreaker:
    """Trip after N failures inside the reset window; resets after cooldown."""

    failure_threshold: int = 5
    reset_timeout_s: int = 60
    _failures: int = 0
    _opened_at: float | None = field(default=None, repr=False)
    _time_fn: "callable[[], float]" = field(default=time.monotonic, repr=False)

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self._time_fn()

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if self._time_fn() - self._opened_at >= self.reset_timeout_s:
            # cool-down elapsed → half-open: clear and give the venue another shot
            self._failures = 0
            self._opened_at = None
            return False
        return True


class SmartRouter:
    """Route orders to the right venue with automatic failover."""

    def __init__(
        self,
        bybit: BybitVenue | None = None,
        okx: OkxVenue | None = None,
        tradovate: TradovateVenue | None = None,
        ibkr: VenueBase | None = None,
    ) -> None:
        self.bybit = bybit or BybitVenue()
        self.okx = okx or OkxVenue()
        self.tradovate = tradovate or TradovateVenue()
        self.ibkr = ibkr  # TODO: wire IBKR venue when built
        self._failover_log: list[dict[str, object]] = []
        self._venue_circuits: dict[str, CircuitBreaker] = {
            self.bybit.name: CircuitBreaker(),
            self.okx.name: CircuitBreaker(),
            self.tradovate.name: CircuitBreaker(),
        }
        if self.ibkr is not None:
            self._venue_circuits[self.ibkr.name] = CircuitBreaker()

    # ----- routing -----

    def choose_venue(
        self,
        symbol: str,
        quantity: float = 0.0,
        urgency: Urgency = "normal",
    ) -> VenueBase:
        """Pick the primary venue for an order."""
        _ = quantity, urgency  # reserved for future size/urgency tuning
        if _is_futures(symbol):
            return self.tradovate
        if _is_crypto(symbol):
            return self.bybit
        return self.bybit

    def _fallback_for(self, primary: VenueBase) -> VenueBase | None:
        if primary is self.bybit:
            return self.okx
        if primary is self.tradovate:
            return self.ibkr
        if primary is self.okx:
            return self.bybit
        return None

    # ----- execution -----

    async def place_with_failover(
        self,
        req: OrderRequest,
        max_attempts: int = 2,
        urgency: Urgency = "normal",
    ) -> OrderResult:
        """Try primary; on REJECTED or exception, try fallback (up to max_attempts)."""
        primary = self.choose_venue(req.symbol, req.qty, urgency)
        attempted: list[VenueBase] = []
        last_exc: Exception | None = None
        last_result: OrderResult | None = None

        venue: VenueBase | None = primary
        while venue is not None and len(attempted) < max_attempts:
            circuit = self._venue_circuits.get(venue.name)
            if circuit is not None and circuit.is_open():
                logger.warning("circuit OPEN for venue=%s, skipping", venue.name)
                self._failover_log.append({"venue": venue.name, "reason": "circuit_open", "ts": time.time()})
                attempted.append(venue)
                venue = self._fallback_for(venue)
                continue
            try:
                result = await venue.place_order(req)
                if result.status is OrderStatus.REJECTED:
                    if circuit is not None:
                        circuit.record_failure()
                    self._failover_log.append(
                        {"venue": venue.name, "reason": "rejected", "ts": time.time(), "order_id": result.order_id}
                    )
                    last_result = result
                    attempted.append(venue)
                    venue = self._fallback_for(venue)
                    continue
                if circuit is not None:
                    circuit.record_success()
                return result
            except Exception as exc:  # noqa: BLE001
                if circuit is not None:
                    circuit.record_failure()
                last_exc = exc
                self._failover_log.append(
                    {"venue": venue.name, "reason": "exception", "err": str(exc), "ts": time.time()}
                )
                logger.warning("venue=%s raised %s; trying fallback", venue.name, exc)
                attempted.append(venue)
                venue = self._fallback_for(venue)

        if last_result is not None:
            return last_result
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("place_with_failover: no venue attempted")
