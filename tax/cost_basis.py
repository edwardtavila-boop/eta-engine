"""
EVOLUTIONARY TRADING ALGO  //  tax.cost_basis
=================================
Lot-tracking cost-basis engine. Supports FIFO / LIFO / HIFO / specific-ID.
Tracks lots per (asset, account_tier). Handles partial fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from eta_engine.tax.models import (
    AccountTier,
    EventType,
    InstrumentType,
    TaxableEvent,
)

if TYPE_CHECKING:
    from datetime import datetime

Method = Literal["FIFO", "LIFO", "HIFO", "SPEC_ID"]


@dataclass
class Lot:
    lot_id: str
    asset: str
    qty: float
    price: float
    timestamp: datetime
    fees: float = 0.0

    @property
    def cost_basis(self) -> float:
        return self.qty * self.price + self.fees


@dataclass
class _Book:
    lots: list[Lot] = field(default_factory=list)
    lot_counter: int = 0


class CostBasisCalculator:
    """Multi-asset, multi-tier cost-basis engine."""

    def __init__(self, method: Method = "FIFO") -> None:
        self.method: Method = method
        self._books: dict[tuple[str, AccountTier], _Book] = {}
        self._event_counter: int = 0

    # ------------------------------------------------------------------
    # Buys
    # ------------------------------------------------------------------
    def add_buy(
        self,
        asset: str,
        qty: float,
        price: float,
        timestamp: datetime,
        fees: float = 0.0,
        account_tier: AccountTier = AccountTier.US,
    ) -> Lot:
        if qty <= 0:
            raise ValueError("qty must be positive for buy")
        book = self._book(asset, account_tier)
        book.lot_counter += 1
        lot = Lot(
            lot_id=f"{asset}-{account_tier.value}-{book.lot_counter}",
            asset=asset,
            qty=qty,
            price=price,
            timestamp=timestamp,
            fees=fees,
        )
        book.lots.append(lot)
        return lot

    # ------------------------------------------------------------------
    # Sells (with lot matching)
    # ------------------------------------------------------------------
    def process_sell(
        self,
        asset: str,
        qty: float,
        price: float,
        timestamp: datetime,
        fees: float = 0.0,
        account_tier: AccountTier = AccountTier.US,
        instrument_type: InstrumentType = InstrumentType.CRYPTO_SPOT,
        lot_id: str | None = None,
    ) -> list[TaxableEvent]:
        if qty <= 0:
            raise ValueError("qty must be positive for sell")
        book = self._book(asset, account_tier)
        remaining = qty
        events: list[TaxableEvent] = []
        total_lots = len(book.lots)
        if total_lots == 0:
            raise ValueError(f"No lots available for {asset} on {account_tier}")
        # Build priority order of lots
        order = self._lot_priority(book.lots, lot_id)
        # Allocate fees across realized portion proportionally
        for lot in order:
            if remaining <= 0:
                break
            take = min(remaining, lot.qty)
            frac = take / lot.qty if lot.qty > 0 else 1.0
            lot_cost = (lot.price * take) + (lot.fees * frac)
            sell_fee = fees * (take / qty)
            proceeds = (price * take) - sell_fee
            gain = proceeds - lot_cost
            holding = max((timestamp - lot.timestamp).days, 0)
            self._event_counter += 1
            events.append(
                TaxableEvent(
                    event_id=f"sell-{self._event_counter}",
                    timestamp=timestamp,
                    event_type=EventType.TRADE_CLOSE,
                    asset=asset,
                    qty=take,
                    cost_basis_usd=round(lot_cost, 6),
                    proceeds_usd=round(proceeds, 6),
                    realized_gain_usd=round(gain, 6),
                    holding_days=holding,
                    account_tier=account_tier,
                    instrument_type=instrument_type,
                )
            )
            lot.qty -= take
            lot.fees *= 1.0 - frac
            remaining -= take
        # Evict fully consumed lots
        book.lots = [lot for lot in book.lots if lot.qty > 1e-12]
        if remaining > 1e-9:
            raise ValueError(f"Oversell: {remaining:g} {asset} beyond available lots")
        return events

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def open_lots(
        self,
        asset: str,
        account_tier: AccountTier = AccountTier.US,
    ) -> list[Lot]:
        return list(self._book(asset, account_tier).lots)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _book(self, asset: str, tier: AccountTier) -> _Book:
        key = (asset, tier)
        book = self._books.get(key)
        if book is None:
            book = _Book()
            self._books[key] = book
        return book

    def _lot_priority(self, lots: list[Lot], lot_id: str | None) -> list[Lot]:
        if self.method == "SPEC_ID" and lot_id is not None:
            selected = [lot for lot in lots if lot.lot_id == lot_id]
            if not selected:
                raise ValueError(f"lot_id not found: {lot_id}")
            others = [lot for lot in lots if lot.lot_id != lot_id]
            return selected + others
        if self.method == "FIFO":
            return sorted(lots, key=lambda lot: lot.timestamp)
        if self.method == "LIFO":
            return sorted(lots, key=lambda lot: lot.timestamp, reverse=True)
        if self.method == "HIFO":
            return sorted(lots, key=lambda lot: lot.price, reverse=True)
        # SPEC_ID without lot_id — fall back to FIFO
        return sorted(lots, key=lambda lot: lot.timestamp)
