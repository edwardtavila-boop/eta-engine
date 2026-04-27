"""
EVOLUTIONARY TRADING ALGO  //  tax.models
=============================
Pydantic v2 models for taxable events, reports, and tiered account types.
"""

from __future__ import annotations

import datetime as _datetime_runtime  # noqa: F401  -- pydantic v2 forward-ref resolution
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from datetime import datetime
else:
    datetime = _datetime_runtime.datetime


class EventType(StrEnum):
    TRADE_CLOSE = "TRADE_CLOSE"
    STAKING_RECEIPT = "STAKING_RECEIPT"
    AIRDROP = "AIRDROP"
    FEE = "FEE"
    FUNDING_PAYMENT = "FUNDING_PAYMENT"
    TRANSFER = "TRANSFER"


class AccountTier(StrEnum):
    US = "US"
    OFFSHORE = "OFFSHORE"


class InstrumentType(StrEnum):
    FUTURES_1256 = "FUTURES_1256"
    CRYPTO_SPOT = "CRYPTO_SPOT"
    CRYPTO_PERP = "CRYPTO_PERP"


class TaxableEvent(BaseModel):
    """Single line item for tax reporting (one per closed lot or income receipt)."""

    event_id: str
    timestamp: datetime
    event_type: EventType
    asset: str
    qty: float
    cost_basis_usd: float = Field(ge=0.0)
    proceeds_usd: float
    realized_gain_usd: float
    holding_days: int = Field(ge=0)
    account_tier: AccountTier
    instrument_type: InstrumentType

    @property
    def is_long_term(self) -> bool:
        return self.holding_days >= 365

    @property
    def is_short_term(self) -> bool:
        return not self.is_long_term and self.instrument_type != InstrumentType.FUTURES_1256


class TaxReport(BaseModel):
    """Full annual tax summary across all accounts / instruments."""

    tax_year: int
    total_short_term_gain: float = 0.0
    total_long_term_gain: float = 0.0
    total_section_1256: float = Field(
        default=0.0,
        description="Pre-60/40 futures PnL subject to mark-to-market under IRC 1256",
    )
    total_staking_income: float = 0.0
    trades: list[TaxableEvent] = Field(default_factory=list)
    summary_6040_futures: dict = Field(
        default_factory=dict,
        description="Output of breakdown_60_40 applied to total_section_1256",
    )

    def total_taxable(self) -> float:
        return (
            self.total_short_term_gain + self.total_long_term_gain + self.total_section_1256 + self.total_staking_income
        )
