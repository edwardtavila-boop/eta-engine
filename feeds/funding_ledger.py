"""Crypto perpetual-swap funding-cost ledger.

A perpetual futures contract has no expiry, so the exchange anchors
the contract price to spot via a periodic *funding payment* between
longs and shorts. Binance, Bybit, OKX, and most major venues settle
funding every 8 hours — at 00:00, 08:00, and 16:00 UTC — at a rate
roughly proportional to the (perp - spot) basis. In contango regimes
that rate is positive (longs pay shorts); in backwardation it's
negative (shorts pay longs).

Order of magnitude: a typical positive-funding day prints around
``+0.01%`` per settlement, or ~``11% APR`` for a long held continuously.
A backtest that ignores this systematically over-states the Sharpe of
trend-following crypto longs and under-states the Sharpe of carry
shorts. This module exists to close that gap.

Scope and assumptions
---------------------
* This ledger is opt-in — ``paper_trade_sim`` keeps it OFF by default
  so existing PnL/Sharpe metrics do not change without a deliberate
  config flip.
* Only symbols whose ``InstrumentSpec.is_perpetual`` is ``True`` get
  charged. Non-perp symbols (MNQ, NQ, ES, GC, CL, ...) early-return
  ``$0`` so callers can blanket-invoke the ledger without per-symbol
  branching.
* CME BTC/ETH futures do NOT pay funding — they're cash-settled
  expiring contracts. Until the engine routes perp orders separately
  from CME futures, both BTC and ETH are flagged perpetual and this
  ledger will charge funding when invoked. See the TODO on
  ``InstrumentSpec.is_perpetual``.
* Funding rate is sampled AT each settlement timestamp. Variable
  intra-settlement rates (which Binance does publish) are out of
  scope; one rate per settlement is the right granularity for paper
  PnL since that's what actually clears the user's account.

Math
----
For each 8h settlement S that strictly falls inside ``(entry_ts,
exit_ts]``::

    notional_usd = qty * entry_price          # entry-locked notional
    rate         = funding_provider.rate_at(S)  # NaN if data missing
    payment_usd  = notional_usd * rate * sign   # see sign convention

    sign = -1 for LONG  (longs pay positive funding)
    sign = +1 for SHORT (shorts receive positive funding)

The signed payment is then NEGATED on return so the ledger expresses
*cost to the trader* (positive = cost, negative = credit). ``NaN``
rates skip that settlement without aborting the whole window — better
to under-charge by one tick than to bail and pretend funding doesn't
exist for the remaining settlements.

Public API
----------
::

    ledger = FundingLedger()
    cost_usd = ledger.compute_funding_cost(
        symbol="BTC", side="LONG", qty=1.0,
        entry_price=60000.0,
        entry_ts=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        exit_ts=datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
        funding_provider=provider,  # any callable rate_at(ts) -> float
    )

The provider may be:
* A class with a ``rate_at(datetime) -> float`` method (preferred), OR
* A callable ``func(datetime) -> float``, OR
* A ``FundingRateProvider`` instance from ``macro_confluence_providers``
  — the ledger wraps it via a tiny shim that builds a synthetic ``bar``
  with the right ``timestamp``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from eta_engine.feeds.instrument_specs import get_spec

UTC = timezone.utc

# Binance/Bybit/OKX schedule: settlements at 00:00, 08:00, 16:00 UTC.
# Expressed as the hour-of-day; minutes/seconds always zero.
_FUNDING_SETTLEMENT_HOURS_UTC: tuple[int, ...] = (0, 8, 16)


@runtime_checkable
class _RateAtProvider(Protocol):
    """Provider exposing ``rate_at(datetime) -> float``."""
    def rate_at(self, ts: datetime) -> float: ...


@dataclass(frozen=True)
class FundingSettlement:
    """One booked funding settlement during a position's lifetime."""
    settlement_ts: datetime
    funding_rate: float          # raw published rate (e.g. 0.0001 = 0.01%)
    notional_usd: float          # qty * entry_price
    payment_usd: float           # signed: positive = cost to trader, negative = credit


class FundingLedger:
    """Computes the dollar funding cost of a perp position over its life.

    Stateless — instances are cheap to create; nothing is cached
    across ``compute_funding_cost`` calls. Construct one per
    simulation run (or per trade) and discard.
    """

    def __init__(
        self,
        settlement_hours_utc: Iterable[int] = _FUNDING_SETTLEMENT_HOURS_UTC,
    ) -> None:
        # Sorted, deduped, validated to 0..23.
        hours = sorted({int(h) for h in settlement_hours_utc})
        for h in hours:
            if not 0 <= h <= 23:
                raise ValueError(
                    f"settlement_hours_utc must be in [0,23]; got {h}",
                )
        if not hours:
            raise ValueError("settlement_hours_utc must be non-empty")
        self._settlement_hours: tuple[int, ...] = tuple(hours)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_funding_cost(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        entry_ts: datetime,
        exit_ts: datetime,
        funding_provider: Any,
    ) -> float:
        """Total funding paid (positive) or received (negative) in USD.

        Returns ``$0`` for non-perp symbols, zero-qty positions, or
        windows that contain no settlements. ``NaN`` rates from the
        provider are skipped.

        Parameters
        ----------
        symbol: instrument symbol, e.g. ``"BTC"``, ``"MNQ"``.
        side: ``"LONG"`` or ``"SHORT"`` (case-insensitive).
        qty: position size in contracts/units (positive number).
        entry_price: fill price at entry — used as the notional anchor.
        entry_ts, exit_ts: timezone-aware datetimes (UTC recommended).
        funding_provider: anything with ``rate_at(ts) -> float``,
            or a plain callable ``ts -> float``, or a
            ``FundingRateProvider``-style object that takes a bar with
            ``.timestamp``. NaN responses are treated as missing data.
        """
        settlements = self._book_settlements(
            symbol=symbol, side=side, qty=qty, entry_price=entry_price,
            entry_ts=entry_ts, exit_ts=exit_ts,
            funding_provider=funding_provider,
        )
        return sum(s.payment_usd for s in settlements)

    def book_settlements(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        entry_ts: datetime,
        exit_ts: datetime,
        funding_provider: Any,
    ) -> list[FundingSettlement]:
        """Same logic as ``compute_funding_cost`` but returns the
        per-settlement breakdown for audit / per-trade printout.
        """
        return self._book_settlements(
            symbol=symbol, side=side, qty=qty, entry_price=entry_price,
            entry_ts=entry_ts, exit_ts=exit_ts,
            funding_provider=funding_provider,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _book_settlements(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        entry_ts: datetime,
        exit_ts: datetime,
        funding_provider: Any,
    ) -> list[FundingSettlement]:
        # Early exits: cheap guards so callers don't have to branch.
        if not self._is_perpetual(symbol):
            return []
        if qty <= 0 or entry_price <= 0:
            return []
        if exit_ts <= entry_ts:
            return []

        sign = self._sign_for_side(side)
        if sign == 0:
            return []

        notional_usd = qty * entry_price
        rate_lookup = self._coerce_provider(funding_provider)
        out: list[FundingSettlement] = []

        for s_ts in self._iter_settlements(entry_ts, exit_ts):
            try:
                rate = float(rate_lookup(s_ts))
            except (TypeError, ValueError):
                continue
            if math.isnan(rate):
                continue
            # sign:  LONG  -> -1 (longs pay positive funding)  =>  cost = +notional*rate
            #        SHORT -> +1 (shorts receive positive funding) => cost = -notional*rate
            payment_usd = -sign * notional_usd * rate
            out.append(FundingSettlement(
                settlement_ts=s_ts,
                funding_rate=rate,
                notional_usd=notional_usd,
                payment_usd=payment_usd,
            ))
        return out

    @staticmethod
    def _is_perpetual(symbol: str) -> bool:
        try:
            spec = get_spec(symbol)
        except Exception:  # noqa: BLE001 — safe default for unknown symbols
            return False
        return bool(getattr(spec, "is_perpetual", False))

    @staticmethod
    def _sign_for_side(side: str) -> int:
        s = (side or "").strip().upper()
        if s == "LONG":
            return -1
        if s == "SHORT":
            return +1
        return 0

    def _iter_settlements(
        self,
        entry_ts: datetime,
        exit_ts: datetime,
    ) -> Iterable[datetime]:
        """Yield settlement timestamps strictly inside ``(entry_ts, exit_ts]``.

        A settlement that lands EXACTLY on entry_ts is excluded
        (entered after the booking) and one that lands exactly on
        exit_ts is included (still on the book at close). This matches
        how exchanges credit/debit accounts at the settlement instant.
        """
        # Walk one day at a time starting from entry_ts's date,
        # emitting each configured hour. Bounded by exit_ts.
        if entry_ts.tzinfo is None or exit_ts.tzinfo is None:
            # Defensive: assume UTC if naive — better than crashing,
            # documented at the public boundary as "UTC recommended".
            entry_ts = entry_ts.replace(tzinfo=UTC) if entry_ts.tzinfo is None else entry_ts
            exit_ts = exit_ts.replace(tzinfo=UTC) if exit_ts.tzinfo is None else exit_ts

        # Start from midnight UTC of entry_ts's date so we don't skip
        # settlements on the same day as entry.
        cursor_date = entry_ts.astimezone(UTC).date()
        end_date = exit_ts.astimezone(UTC).date()
        # Iterate at most (end_date - cursor_date + 1) days; bound the
        # loop defensively to prevent runaway if exit_ts is unbounded.
        max_days = (end_date - cursor_date).days + 2
        for _ in range(max_days):
            for h in self._settlement_hours:
                s_ts = datetime(
                    cursor_date.year, cursor_date.month, cursor_date.day,
                    h, 0, 0, tzinfo=UTC,
                )
                if s_ts <= entry_ts:
                    continue
                if s_ts > exit_ts:
                    return
                yield s_ts
            cursor_date += timedelta(days=1)

    @staticmethod
    def _coerce_provider(provider: Any) -> Callable[[datetime], float]:
        """Wrap any of the supported provider shapes into a uniform
        ``ts -> float`` callable.

        Order of attempts:
        1. ``provider.rate_at(ts)`` — preferred, explicit.
        2. ``provider(ts)`` — plain callable taking a datetime.
        3. ``provider(_BarShim(ts))`` — fall back to the
           ``FundingRateProvider`` shape which expects ``bar.timestamp``.
        """
        if hasattr(provider, "rate_at") and callable(provider.rate_at):
            return lambda ts: float(provider.rate_at(ts))

        if not callable(provider):
            raise TypeError(
                "funding_provider must expose rate_at(ts) or be callable; "
                f"got {type(provider).__name__}",
            )

        def _lookup(ts: datetime) -> float:
            # Try the plain (ts) signature first; on failure, fall back
            # to the bar-shaped signature used by FundingRateProvider.
            try:
                return float(provider(ts))
            except (AttributeError, TypeError):
                shim = _BarShim(ts)
                return float(provider(shim))

        return _lookup


@dataclass(frozen=True)
class _BarShim:
    """Minimal stand-in for a ``BarData`` that exposes ``.timestamp``.

    Used to call ``FundingRateProvider`` instances which were written
    to consume strategy bars, not raw datetimes. Keeps this module
    free of an import on ``core.data_pipeline.BarData``.
    """
    timestamp: datetime
