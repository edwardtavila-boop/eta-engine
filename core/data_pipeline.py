"""
EVOLUTIONARY TRADING ALGO  //  data_pipeline
================================
Async data feed abstraction.
One interface. Multiple venues. Zero excuses.
"""

from __future__ import annotations

import datetime as _datetime_runtime  # noqa: F401  -- pydantic v2 forward-ref resolution
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from datetime import datetime
else:
    datetime = _datetime_runtime.datetime

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class BarData(BaseModel):
    """OHLCV bar."""

    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class L2Snapshot(BaseModel):
    """Level 2 order book snapshot."""

    timestamp: datetime
    symbol: str
    bids: list[list[float]] = Field(
        default_factory=list,
        description="[[price, qty], ...] best bid first",
    )
    asks: list[list[float]] = Field(
        default_factory=list,
        description="[[price, qty], ...] best ask first",
    )

    @property
    def spread(self) -> float | None:
        if self.bids and self.asks:
            return self.asks[0][0] - self.bids[0][0]
        return None

    @property
    def mid_price(self) -> float | None:
        if self.bids and self.asks:
            return (self.asks[0][0] + self.bids[0][0]) / 2.0
        return None


class FundingRate(BaseModel):
    """Perpetual funding rate snapshot."""

    timestamp: datetime
    symbol: str
    rate: float = Field(description="Current funding rate (decimal)")
    predicted_rate: float | None = Field(default=None, description="Predicted next funding rate")
    next_funding_time: datetime | None = None


BarCallback = Callable[[BarData], Coroutine[Any, Any, None]]
TickCallback = Callable[[BarData], Coroutine[Any, Any, None]]
L2Callback = Callable[[L2Snapshot], Coroutine[Any, Any, None]]


class DataFeed(ABC):
    """Base class for all venue data feeds."""

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._connected: bool = False
        self._subscriptions: list[str] = []

    @property
    def connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def connect(self) -> None:
        """Establish websocket / REST session."""
        ...

    @abstractmethod
    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to market data for given symbols."""
        ...

    @abstractmethod
    async def on_bar(self, callback: BarCallback) -> None:
        """Register OHLCV bar callback."""
        ...

    @abstractmethod
    async def on_tick(self, callback: TickCallback) -> None:
        """Register tick-level trade callback."""
        ...

    @abstractmethod
    async def on_l2(self, callback: L2Callback) -> None:
        """Register L2 orderbook callback."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean shutdown."""
        ...


class BybitFeed(DataFeed):
    """Bybit perpetuals data feed.

    TODO: implement websocket connection via pybit / raw ws
    TODO: handle rate limits + reconnection
    TODO: map Bybit kline intervals to internal BarData
    """

    WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"

    async def connect(self) -> None:
        # TODO: open websocket to self.WS_PUBLIC
        self._connected = True

    async def subscribe(self, symbols: list[str]) -> None:
        self._subscriptions.extend(symbols)
        # TODO: send subscribe message for each symbol

    async def on_bar(self, callback: BarCallback) -> None:
        # TODO: wire kline messages to callback
        pass

    async def on_tick(self, callback: TickCallback) -> None:
        # TODO: wire publicTrade messages to callback
        pass

    async def on_l2(self, callback: L2Callback) -> None:
        # TODO: wire orderbook.50 messages to callback
        pass

    async def disconnect(self) -> None:
        self._connected = False
        self._subscriptions.clear()
        # TODO: close websocket


class TradovateFeed(DataFeed):
    """Tradovate futures data feed.

    TODO: implement OAuth2 token flow
    TODO: connect to Tradovate market data websocket
    TODO: handle contract rollover (MNQ front month)
    """

    WS_MARKET = "wss://md.tradovateapi.com/v1/websocket"

    async def connect(self) -> None:
        # TODO: authenticate via OAuth2 then open ws
        self._connected = True

    async def subscribe(self, symbols: list[str]) -> None:
        self._subscriptions.extend(symbols)
        # TODO: send md/subscribeQuote for each symbol

    async def on_bar(self, callback: BarCallback) -> None:
        # TODO: aggregate ticks into bars or use chart endpoint
        pass

    async def on_tick(self, callback: TickCallback) -> None:
        # TODO: wire quote change messages
        pass

    async def on_l2(self, callback: L2Callback) -> None:
        # TODO: wire md/subscribeDOM messages
        pass

    async def disconnect(self) -> None:
        self._connected = False
        self._subscriptions.clear()
        # TODO: close websocket + revoke token
