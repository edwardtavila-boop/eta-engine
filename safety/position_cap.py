"""EVOLUTIONARY TRADING ALGO // safety.position_cap.

Per-(side, venue, symbol) contract-count cap. Fail-closed: if the
cap is exceeded the call raises :class:`PositionCapExceeded` and the
caller is expected to NOT route the order.

Caps are read from environment variables on every call so an
operator can tighten them at runtime without restarting the bot.
The lookup order for a given (side, venue, symbol) is:

1. ``APEX_POSITION_CAP_<SIDE>_<VENUE>_<SYMBOL>`` (most specific)
2. ``APEX_POSITION_CAP_<SIDE>_<VENUE>``
3. ``APEX_POSITION_CAP_<SIDE>``
4. ``APEX_POSITION_CAP`` (global default)
5. :data:`DEFAULT_CAP` -- the conservative fallback

The Apex eval mandates a cap of 1 contract. The default fallback
here is intentionally generous (10) so paper / test paths route
freely; the LIVE deployment sets ``APEX_POSITION_CAP=1`` (or per-
side overrides) explicitly via the operator manifest.

This module tracks ONLY the requested-delta against a cap. Tracking
the running net position is the bot's job (see ``BotState.position``
and the broker reconciler); this gate just refuses any single
order whose absolute size exceeds the cap.
"""

from __future__ import annotations

import os


DEFAULT_CAP: float = 10.0


class PositionCapExceeded(RuntimeError):
    """Raised when an order would exceed the configured contract cap.

    The exception's ``.cap`` and ``.requested`` attributes carry the
    numeric values for structured logging.
    """

    def __init__(
        self,
        message: str,
        *,
        cap: float,
        requested: float,
        side: str,
        venue: str,
        symbol: str,
    ) -> None:
        super().__init__(message)
        self.cap = cap
        self.requested = requested
        self.side = side
        self.venue = venue
        self.symbol = symbol


def _resolve_cap(side: str, venue: str, symbol: str) -> float:
    """Walk the env-var hierarchy and return the most-specific cap.

    Returns :data:`DEFAULT_CAP` when nothing matches.
    """
    side_u = side.upper()
    venue_u = venue.upper()
    symbol_u = symbol.upper()
    for key in (
        f"APEX_POSITION_CAP_{side_u}_{venue_u}_{symbol_u}",
        f"APEX_POSITION_CAP_{side_u}_{venue_u}",
        f"APEX_POSITION_CAP_{side_u}",
        "APEX_POSITION_CAP",
    ):
        raw = os.environ.get(key)
        if raw is None or not raw.strip():
            continue
        try:
            return float(raw)
        except ValueError:
            # An invalid env-var entry should NOT silently fall through
            # to a more permissive cap -- but the gate's job is to
            # protect downstream order routing, not to validate
            # operator config. Skip and continue to the next layer.
            continue
    return DEFAULT_CAP


def assert_within_caps(
    *,
    side: str,
    venue: str,
    symbol: str,
    requested_delta: float,
) -> None:
    """Pass when ``|requested_delta| <= cap``; raise otherwise.

    ``requested_delta`` is signed (positive = increase exposure on
    ``side``, negative = reduce / flip). The cap is checked against
    the absolute value so a SELL of 5 against a cap of 4 raises
    just like a BUY of 5 would.
    """
    cap = _resolve_cap(side, venue, symbol)
    if abs(float(requested_delta)) > cap:
        raise PositionCapExceeded(
            (
                f"position cap exceeded: side={side} venue={venue} "
                f"symbol={symbol} requested={requested_delta} cap={cap}"
            ),
            cap=cap,
            requested=float(requested_delta),
            side=side,
            venue=venue,
            symbol=symbol,
        )


__all__ = [
    "DEFAULT_CAP",
    "PositionCapExceeded",
    "assert_within_caps",
]
