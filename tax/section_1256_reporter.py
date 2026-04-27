"""
EVOLUTIONARY TRADING ALGO  //  tax.section_1256_reporter
============================================
Section 1256 contracts (futures) — 60/40 long/short treatment + Form 6781 summary.
Mark-to-market applies to open positions at year end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eta_engine.tax.models import (
    AccountTier,
    EventType,
    InstrumentType,
    TaxableEvent,
)

if TYPE_CHECKING:
    from datetime import datetime


@dataclass
class OpenFuturesPosition:
    """A still-open futures position at year end (needs MTM)."""

    symbol: str
    qty: float
    entry_price: float
    entry_time: datetime
    account_tier: AccountTier = AccountTier.US


class Section1256Reporter:
    """60/40 treatment for IRC 1256 contracts (futures + non-equity options)."""

    LONG_TERM_FRAC: float = 0.60
    SHORT_TERM_FRAC: float = 0.40

    # ------------------------------------------------------------------
    # Mark-to-market open positions
    # ------------------------------------------------------------------
    def mark_to_market(
        self,
        positions: list[OpenFuturesPosition],
        year_end_date: datetime,
        year_end_prices: dict[str, float],
    ) -> list[TaxableEvent]:
        """Convert open futures positions to realized events via MTM.

        IRC 1256 treats open contracts as if sold at fair market value on the
        last business day of the tax year. Resulting gain is subject to 60/40.
        """
        events: list[TaxableEvent] = []
        for i, pos in enumerate(positions):
            mtm_price = year_end_prices.get(pos.symbol)
            if mtm_price is None:
                continue
            proceeds = mtm_price * pos.qty
            cost = pos.entry_price * pos.qty
            gain = proceeds - cost
            holding = max((year_end_date - pos.entry_time).days, 0)
            events.append(
                TaxableEvent(
                    event_id=f"mtm-{pos.symbol}-{i}",
                    timestamp=year_end_date,
                    event_type=EventType.TRADE_CLOSE,
                    asset=pos.symbol,
                    qty=pos.qty,
                    cost_basis_usd=round(cost, 2),
                    proceeds_usd=round(proceeds, 2),
                    realized_gain_usd=round(gain, 2),
                    holding_days=holding,
                    account_tier=pos.account_tier,
                    instrument_type=InstrumentType.FUTURES_1256,
                )
            )
        return events

    # ------------------------------------------------------------------
    # 60/40 breakdown
    # ------------------------------------------------------------------
    def breakdown_60_40(self, total_gain: float) -> dict[str, float]:
        long_term = total_gain * self.LONG_TERM_FRAC
        short_term = total_gain * self.SHORT_TERM_FRAC
        return {
            "long_term_60": round(long_term, 2),
            "short_term_40": round(short_term, 2),
            "total": round(total_gain, 2),
        }

    # ------------------------------------------------------------------
    # Form 6781 summary
    # ------------------------------------------------------------------
    def generate_form_6781_summary(
        self,
        events: list[TaxableEvent],
    ) -> dict[str, Any]:
        """Generate Form 6781 Part I line items.

        Line 1: individual 1256 contracts w/ gain/loss
        Line 2: sum of amounts on line 1
        Line 3: net gain/loss
        Line 5: (no 60/40 election applied — defaults)
        Line 8: short-term 40% portion -> Schedule D line
        Line 9: long-term 60% portion -> Schedule D line
        """
        contracts_1256 = [e for e in events if e.instrument_type == InstrumentType.FUTURES_1256]
        line1_items = [
            {
                "description": f"{e.asset} ({e.qty:g})",
                "gain_loss": round(e.realized_gain_usd, 2),
            }
            for e in contracts_1256
        ]
        total = sum(e.realized_gain_usd for e in contracts_1256)
        breakdown = self.breakdown_60_40(total)
        return {
            "line_1_contracts": line1_items,
            "line_2_sum": round(total, 2),
            "line_3_net": round(total, 2),
            "line_8_short_term_40pct": breakdown["short_term_40"],
            "line_9_long_term_60pct": breakdown["long_term_60"],
            "n_contracts": len(contracts_1256),
        }
