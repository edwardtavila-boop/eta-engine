"""
EVOLUTIONARY TRADING ALGO  //  venues.base
==============================
VenueBase contract + order request/result models.
Every execution surface subclasses this. No direct SDK leaks upstream.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    POST_ONLY = "POST_ONLY"


class OrderStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    OPEN = "OPEN"
    REJECTED = "REJECTED"


class OrderRequest(BaseModel):
    """Venue-agnostic order spec."""

    symbol: str
    side: Side
    qty: float = Field(gt=0.0)
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    reduce_only: bool = False
    client_order_id: str | None = None


class OrderResult(BaseModel):
    """Venue-agnostic order execution result."""

    order_id: str
    status: OrderStatus
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fees: float = 0.0
    latency_ms: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)


class VenueBase(ABC):
    """Abstract trading venue surface."""

    name: str = "base"

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        self.api_key = api_key
        self.api_secret = api_secret

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult:
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def get_balance(self) -> dict[str, float]:
        ...

    def idempotency_key(self, request: OrderRequest) -> str:
        """Deterministic client order id from the request payload."""
        if request.client_order_id:
            return request.client_order_id
        payload = "|".join(
            [
                self.name,
                request.symbol,
                request.side.value,
                f"{request.qty:.8f}",
                request.order_type.value,
                f"{request.price or 0.0:.8f}",
                str(request.reduce_only),
            ]
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]
