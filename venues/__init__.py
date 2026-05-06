"""
EVOLUTIONARY TRADING ALGO  //  venues
=========================
Execution surfaces. One interface, multiple exchanges.

Crypto paper (active): Alpaca paper (BTC/ETH/SOL/XRP/AVAX/LINK/DOGE
plus 16 more bases — added 2026-05-05 while Tastytrade cert sandbox
crypto enablement is pending operator action).

Crypto offshore (dev / non-US-person only): Bybit + OKX.

Futures live (active): IBKR + Tastytrade.

Futures live (DORMANT): Tradovate -- funding-blocked until further
notice per operator mandate 2026-04-24. Adapter remains importable
and testable; live routing transparently substitutes to IBKR. See
:mod:`eta_engine.venues.router` for the dormancy policy.
"""

from eta_engine.venues.alpaca import (
    AlpacaConfig,
    AlpacaVenue,
    alpaca_paper_readiness,
)
from eta_engine.venues.base import (
    ConnectionStatus,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    VenueBase,
    VenueConnectionReport,
)
from eta_engine.venues.bybit import BybitVenue
from eta_engine.venues.connection import (
    BrokerConnectionManager,
    BrokerConnectionSummary,
    write_broker_connection_report,
)
from eta_engine.venues.ibkr import (
    IbkrClientPortalConfig,
    IbkrClientPortalVenue,
    ibkr_paper_readiness,
)
from eta_engine.venues.okx import OkxVenue
from eta_engine.venues.router import (
    ACTIVE_FUTURES_VENUES,
    DEFAULT_CRYPTO_VENUE,
    DEFAULT_FUTURES_VENUE,
    DORMANT_BROKERS,
    SmartRouter,
)
from eta_engine.venues.tastytrade import (
    TastytradeConfig,
    TastytradeVenue,
    tastytrade_paper_readiness,
)
from eta_engine.venues.tradovate import TradovateVenue

__all__ = [
    "ACTIVE_FUTURES_VENUES",
    "DEFAULT_CRYPTO_VENUE",
    "DEFAULT_FUTURES_VENUE",
    "DORMANT_BROKERS",
    "AlpacaConfig",
    "AlpacaVenue",
    "BybitVenue",
    "BrokerConnectionManager",
    "BrokerConnectionSummary",
    "ConnectionStatus",
    "IbkrClientPortalConfig",
    "IbkrClientPortalVenue",
    "OkxVenue",
    "OrderRequest",
    "OrderResult",
    "OrderStatus",
    "OrderType",
    "Side",
    "SmartRouter",
    "TastytradeConfig",
    "TastytradeVenue",
    "TradovateVenue",
    "VenueBase",
    "VenueConnectionReport",
    "alpaca_paper_readiness",
    "ibkr_paper_readiness",
    "tastytrade_paper_readiness",
    "write_broker_connection_report",
]
