"""Tests for the per-(venue, symbol) execution-capability abstraction.

Pins the behavior surface the supervisor reads to drive correct
lifecycle behavior per venue + asset class — bracket style, cost-basis
minima, session-aware order conversion. Catches drift if a venue's
capability profile changes (e.g. Alpaca crypto support evolves to
allow advanced order classes).
"""

from __future__ import annotations

from eta_engine.venues import (
    AlpacaConfig,
    AlpacaVenue,
    BybitVenue,
    OkxVenue,
    TastytradeConfig,
    TastytradeVenue,
    TradovateVenue,
)
from eta_engine.venues.base import BracketStyle, ExecutionCapabilities

# ---------------------------------------------------------------------------
# Default behavior on VenueBase + venues without an override
# ---------------------------------------------------------------------------


def test_default_capabilities_are_server_oco() -> None:
    """Venues without an override get a conservative SERVER_OCO profile."""
    venue = BybitVenue()  # no override; falls through to base default
    caps = venue.execution_capabilities_for("BTCUSDT")
    assert isinstance(caps, ExecutionCapabilities)
    assert caps.bracket_style == BracketStyle.SERVER_OCO
    assert caps.min_cost_basis_usd == 0.0
    assert caps.supports_reduce_only is True


def test_okx_default_capabilities_match_base() -> None:
    venue = OkxVenue()
    caps = venue.execution_capabilities_for("ETHUSDT")
    assert caps.bracket_style == BracketStyle.SERVER_OCO


def test_tradovate_default_capabilities_match_base() -> None:
    venue = TradovateVenue()
    caps = venue.execution_capabilities_for("MNQ")
    assert caps.bracket_style == BracketStyle.SERVER_OCO


def test_tastytrade_default_capabilities_match_base() -> None:
    """Tastytrade hasn't been split per-asset yet; uses base default."""
    venue = TastytradeVenue(TastytradeConfig(account_number="5WT0000", session_token="t"))
    caps = venue.execution_capabilities_for("BTCUSDT")
    assert caps.bracket_style == BracketStyle.SERVER_OCO


# ---------------------------------------------------------------------------
# AlpacaVenue: crypto vs equity differ
# ---------------------------------------------------------------------------


def test_alpaca_crypto_capabilities_use_supervisor_local_bracket() -> None:
    """Alpaca crypto rejects advanced order_class -> SUPERVISOR_LOCAL.

    Caught live 2026-05-06: HTTP 422 ``crypto orders not allowed for
    advanced order_class: otoco``. Supervisor's tick-level _maybe_exit
    must own stop/target watching for crypto.
    """
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    for sym in ("BTC", "BTCUSDT", "BTC/USD", "ETH", "SOL", "AVAX", "LINK", "DOGE", "XRP"):
        caps = venue.execution_capabilities_for(sym)
        assert caps.bracket_style == BracketStyle.SUPERVISOR_LOCAL, f"{sym} should be SUPERVISOR_LOCAL on Alpaca crypto"
        assert caps.min_cost_basis_usd == 10.0, f"{sym} crypto should enforce $10 min cost basis"


def test_alpaca_equity_capabilities_use_server_oco_bracket() -> None:
    """Alpaca equity accepts order_class=bracket cleanly -> SERVER_OCO."""
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    for sym in ("SPY", "AAPL", "TSLA", "QQQ", "NVDA"):
        caps = venue.execution_capabilities_for(sym)
        assert caps.bracket_style == BracketStyle.SERVER_OCO, f"{sym} should be SERVER_OCO on Alpaca equity"
        assert caps.min_cost_basis_usd == 0.0, f"{sym} equity has no $10 minimum (crypto-only constraint)"


def test_alpaca_session_aware_routing_disabled() -> None:
    """Alpaca doesn't need session-aware MARKET-to-LIMIT conversion.

    Crypto is 24/7. Equity uses time_in_force=day on the order itself.
    """
    venue = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    assert venue.execution_capabilities_for("BTC").supports_session_aware_routing is False
    assert venue.execution_capabilities_for("SPY").supports_session_aware_routing is False


# ---------------------------------------------------------------------------
# LiveIbkrVenue: futures vs crypto on PAXOS
# ---------------------------------------------------------------------------


def test_ibkr_futures_capabilities_are_server_oco_session_aware() -> None:
    """IBKR futures: server-side OCO bracket + session-aware order conversion."""
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    for sym in ("MNQ", "ES", "NQ", "CL", "GC", "M6E", "RTY"):
        caps = venue.execution_capabilities_for(sym)
        assert caps.bracket_style == BracketStyle.SERVER_OCO, f"{sym} futures should be SERVER_OCO on IBKR"
        assert caps.supports_session_aware_routing is True, (
            f"{sym} futures should support session-aware routing on IBKR"
        )


def test_ibkr_crypto_capabilities_use_supervisor_local() -> None:
    """IBKR PAXOS crypto rejects bracket attach -> SUPERVISOR_LOCAL."""
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    venue = LiveIbkrVenue()
    for sym in ("BTC", "BTCUSD", "ETH", "ETHUSD", "SOL", "SOLUSD"):
        caps = venue.execution_capabilities_for(sym)
        assert caps.bracket_style == BracketStyle.SUPERVISOR_LOCAL, f"{sym} should be SUPERVISOR_LOCAL on IBKR crypto"
        assert caps.supports_session_aware_routing is False, f"{sym} crypto: 24/7, no session-aware routing"


# ---------------------------------------------------------------------------
# Cross-venue: the supervisor can use this surface to decide tick-watching
# ---------------------------------------------------------------------------


def test_supervisor_local_bracket_implies_tick_watch_required() -> None:
    """When bracket_style is SUPERVISOR_LOCAL, the supervisor's
    _maybe_exit tick watch is the only protection. Test pins the
    contract: any venue+symbol returning SUPERVISOR_LOCAL must mean
    'watch this position locally'.
    """
    alpaca = AlpacaVenue(AlpacaConfig(api_key_id="PK1", api_secret_key="SECRET1"))
    crypto_caps = alpaca.execution_capabilities_for("BTC")
    # The contract: SUPERVISOR_LOCAL means stop/target are still
    # populated on the OrderRequest (so supervisor knows where to bail)
    # but NOT shipped to the broker. Caller code branches on
    # bracket_style to decide whether to enable tick-level watching.
    assert crypto_caps.bracket_style == BracketStyle.SUPERVISOR_LOCAL
    # And reduce_only is supported for the local exit shipping a sell.
    assert crypto_caps.supports_reduce_only is True


def test_server_oco_bracket_does_not_require_tick_watch() -> None:
    """SERVER_OCO means broker holds the bracket; supervisor needn't watch."""
    from eta_engine.venues.ibkr_live import LiveIbkrVenue

    futures_caps = LiveIbkrVenue().execution_capabilities_for("MNQ")
    assert futures_caps.bracket_style == BracketStyle.SERVER_OCO
