"""
APEX PREDATOR  //  venues
=========================
Execution surfaces. One interface, multiple exchanges.

Crypto: Bybit + OKX.

Futures live (active): IBKR + Tastytrade.

Futures live (DORMANT): Tradovate -- funding-blocked until further
notice per operator mandate 2026-04-24. Adapter remains importable
and testable; live routing transparently substitutes to IBKR. See
:mod:`apex_predator.venues.router` for the dormancy policy.
"""

from apex_predator.venues.base import (
    ConnectionStatus,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    VenueBase,
    VenueConnectionReport,
)
from apex_predator.venues.bybit import BybitVenue
from apex_predator.venues.connection import (
    BrokerConnectionManager,
    BrokerConnectionSummary,
    write_broker_connection_report,
)
from apex_predator.venues.ibkr import (
    IbkrClientPortalConfig,
    IbkrClientPortalVenue,
    ibkr_paper_readiness,
)
from apex_predator.venues.okx import OkxVenue
from apex_predator.venues.router import (
    ACTIVE_FUTURES_VENUES,
    DEFAULT_CRYPTO_VENUE,
    DEFAULT_FUTURES_VENUE,
    DORMANT_BROKERS,
    SmartRouter,
)
from apex_predator.venues.tastytrade import (
    TastytradeConfig,
    TastytradeVenue,
    tastytrade_paper_readiness,
)
from apex_predator.venues.tradovate import TradovateVenue

__all__ = [
    "ACTIVE_FUTURES_VENUES",
    "DEFAULT_CRYPTO_VENUE",
    "DEFAULT_FUTURES_VENUE",
    "DORMANT_BROKERS",
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
    "ibkr_paper_readiness",
    "tastytrade_paper_readiness",
    "write_broker_connection_report",
]
