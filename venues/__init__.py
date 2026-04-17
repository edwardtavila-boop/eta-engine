"""
EVOLUTIONARY TRADING ALGO  //  venues
=========================
Execution surfaces. One interface, multiple exchanges.
Bybit + OKX for crypto. Tradovate + IBKR (stub) for futures.
"""

from eta_engine.venues.base import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    VenueBase,
)
from eta_engine.venues.bybit import BybitVenue
from eta_engine.venues.okx import OkxVenue
from eta_engine.venues.router import SmartRouter
from eta_engine.venues.tradovate import TradovateVenue

__all__ = [
    "BybitVenue",
    "OkxVenue",
    "OrderRequest",
    "OrderResult",
    "OrderStatus",
    "OrderType",
    "Side",
    "SmartRouter",
    "TradovateVenue",
    "VenueBase",
]
