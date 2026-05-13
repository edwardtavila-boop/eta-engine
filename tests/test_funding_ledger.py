"""Behavioral tests for the perpetual-swap funding-cost ledger.

Pin down the contract so paper Sharpe stops over-stating returns on
crypto perp longs held across funding settlements.

Each test corresponds to a class of bug the silent-funding backtest
shipped:

- a 1-day BTC long across 3 settlements at +0.01% funding pays the
  notional times the cumulative rate (positive cost)
- a SHORT in the same scenario receives funding (negative cost)
- a position closed before any settlement pays exactly $0
- NaN funding rates skip without aborting the window
- non-perp symbols (MNQ, NQ) early-return $0 so callers can blanket-
  invoke the ledger without per-symbol branching
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.feeds.funding_ledger import (
    FundingLedger,
    FundingSettlement,
)

UTC = UTC


class _StaticRateProvider:
    """Fixed funding rate at every settlement timestamp."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self.calls: list[datetime] = []

    def rate_at(self, ts: datetime) -> float:
        self.calls.append(ts)
        return self._rate


class _MapRateProvider:
    """Returns rate from a {datetime -> float} map; missing keys -> NaN."""

    def __init__(self, rates: dict[datetime, float]) -> None:
        self._rates = rates

    def rate_at(self, ts: datetime) -> float:
        return self._rates.get(ts, math.nan)


# ── happy path: positive funding ─────────────────────────────────────


def test_one_day_btc_long_across_three_settlements_pays_full_funding():
    """A 1-day BTC long held through 3 × 0.01% settlements pays exactly
    notional * 3 * 0.01% (positive = cost)."""
    ledger = FundingLedger()
    rate = 0.0001  # 0.01% per settlement
    provider = _StaticRateProvider(rate)
    qty = 1.0
    entry_price = 60_000.0
    notional = qty * entry_price  # $60,000

    # Enter just before midnight UTC; exit just after the next midnight
    # so we cross the 00:00, 08:00, 16:00 settlements once each.
    # 24-hour hold from 16:01 UTC to 16:01 UTC the next day strictly
    # contains exactly three settlements: 00:00, 08:00, and 16:00 of
    # the following day. (The 16:00 at entry is *before* entry_ts and
    # the 16:00 at exit IS at exit_ts, which the inclusive-on-exit
    # convention counts.)
    entry_ts = datetime(2026, 5, 1, 16, 1, 0, tzinfo=UTC)
    exit_ts = datetime(2026, 5, 2, 16, 1, 0, tzinfo=UTC)

    cost = ledger.compute_funding_cost(
        symbol="BTC-PERP",
        side="LONG",
        qty=qty,
        entry_price=entry_price,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )

    expected = notional * rate * 3  # $60,000 * 0.0001 * 3 = $18.00
    assert cost == pytest.approx(expected, abs=1e-6)
    assert cost > 0  # LONG with positive funding pays
    assert len(provider.calls) == 3


# ── sign convention: short receives ──────────────────────────────────


def test_short_receives_funding_when_rate_positive():
    """SHORT in the same scenario should receive (negative cost)."""
    ledger = FundingLedger()
    rate = 0.0001
    provider = _StaticRateProvider(rate)
    qty = 1.0
    entry_price = 60_000.0
    notional = qty * entry_price

    # 24-hour hold from 16:01 UTC to 16:01 UTC the next day strictly
    # contains exactly three settlements: 00:00, 08:00, and 16:00 of
    # the following day. (The 16:00 at entry is *before* entry_ts and
    # the 16:00 at exit IS at exit_ts, which the inclusive-on-exit
    # convention counts.)
    entry_ts = datetime(2026, 5, 1, 16, 1, 0, tzinfo=UTC)
    exit_ts = datetime(2026, 5, 2, 16, 1, 0, tzinfo=UTC)

    cost = ledger.compute_funding_cost(
        symbol="BTC-PERP",
        side="SHORT",
        qty=qty,
        entry_price=entry_price,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )

    expected = -notional * rate * 3  # SHORT with positive funding receives
    assert cost == pytest.approx(expected, abs=1e-6)
    assert cost < 0  # SHORT receives — that's a CREDIT, not a cost


# ── early exit: no settlement crossed ────────────────────────────────


def test_position_closed_before_any_settlement_pays_zero():
    """Open at 00:01 UTC, close at 07:59 UTC — no settlement at 08:00
    crossed yet, so funding is $0."""
    ledger = FundingLedger()
    provider = _StaticRateProvider(0.001)  # 0.1% — would be huge if charged

    entry_ts = datetime(2026, 5, 1, 0, 1, 0, tzinfo=UTC)
    exit_ts = datetime(2026, 5, 1, 7, 59, 0, tzinfo=UTC)

    cost = ledger.compute_funding_cost(
        symbol="BTC-PERP",
        side="LONG",
        qty=1.0,
        entry_price=60_000.0,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )

    assert cost == 0.0
    assert provider.calls == []  # provider should not even be queried


# ── missing data: NaN rate skipped ───────────────────────────────────


def test_nan_funding_rates_are_skipped_without_breaking():
    """If a settlement returns NaN (data outage), skip that settlement
    but keep accruing the rest. The whole window should NOT abort."""
    ledger = FundingLedger()
    qty = 1.0
    entry_price = 60_000.0
    notional = qty * entry_price

    # 24-hour hold from 16:01 UTC to 16:01 UTC the next day strictly
    # contains exactly three settlements: 00:00, 08:00, and 16:00 of
    # the following day. (The 16:00 at entry is *before* entry_ts and
    # the 16:00 at exit IS at exit_ts, which the inclusive-on-exit
    # convention counts.)
    entry_ts = datetime(2026, 5, 1, 16, 1, 0, tzinfo=UTC)
    exit_ts = datetime(2026, 5, 2, 16, 1, 0, tzinfo=UTC)

    # Three settlements expected: 2026-05-02 at 00:00, 08:00, 16:00 UTC.
    # Make the middle one NaN to verify it's skipped, not aborted.
    s0 = datetime(2026, 5, 2, 0, 0, 0, tzinfo=UTC)
    s1 = datetime(2026, 5, 2, 8, 0, 0, tzinfo=UTC)
    s2 = datetime(2026, 5, 2, 16, 0, 0, tzinfo=UTC)
    rate = 0.0001
    provider = _MapRateProvider({s0: rate, s1: math.nan, s2: rate})

    cost = ledger.compute_funding_cost(
        symbol="BTC-PERP",
        side="LONG",
        qty=qty,
        entry_price=entry_price,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )

    # Only 2 settlements should book; the NaN one is skipped silently.
    expected = notional * rate * 2  # $12.00
    assert cost == pytest.approx(expected, abs=1e-6)


# ── non-perp early return ────────────────────────────────────────────


@pytest.mark.parametrize("symbol", ["MNQ", "NQ", "ES", "GC", "CL", "BTC", "ETH", "MBT", "MET"])
def test_non_crypto_symbols_get_zero_funding_cost(symbol: str):
    """Futures symbols are not perpetuals — they pay no funding. The
    ledger must early-return $0 so callers can invoke unconditionally."""
    ledger = FundingLedger()
    provider = _StaticRateProvider(0.001)  # would be huge if charged

    entry_ts = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    exit_ts = entry_ts + timedelta(days=7)  # full week — many settlements

    cost = ledger.compute_funding_cost(
        symbol=symbol,
        side="LONG",
        qty=10.0,
        entry_price=22000.0,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )

    assert cost == 0.0
    assert provider.calls == []  # provider must not be queried at all


# ── audit / breakdown surface ────────────────────────────────────────


def test_book_settlements_returns_per_settlement_breakdown():
    """The audit surface ``book_settlements`` exposes one row per
    booked funding event with rate, notional, and signed payment.
    Useful for the per-trade printout in paper_trade_sim."""
    ledger = FundingLedger()
    rate = 0.0002  # 0.02%
    provider = _StaticRateProvider(rate)
    qty = 0.5
    entry_price = 4_000.0  # ETH

    # 24-hour hold from 16:01 UTC to 16:01 UTC the next day strictly
    # contains exactly three settlements: 00:00, 08:00, and 16:00 of
    # the following day. (The 16:00 at entry is *before* entry_ts and
    # the 16:00 at exit IS at exit_ts, which the inclusive-on-exit
    # convention counts.)
    entry_ts = datetime(2026, 5, 1, 16, 1, 0, tzinfo=UTC)
    exit_ts = datetime(2026, 5, 2, 16, 1, 0, tzinfo=UTC)

    rows = ledger.book_settlements(
        symbol="ETH-PERP",
        side="LONG",
        qty=qty,
        entry_price=entry_price,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )

    assert len(rows) == 3
    for row in rows:
        assert isinstance(row, FundingSettlement)
        assert row.funding_rate == pytest.approx(rate)
        assert row.notional_usd == pytest.approx(qty * entry_price)
        assert row.payment_usd == pytest.approx(qty * entry_price * rate)
        assert row.payment_usd > 0  # LONG pays


def test_eth_perp_charges_funding_via_is_perpetual_flag():
    """ETH should be treated as perp (is_perpetual=True) — sanity check
    that the spec flag is what gates funding application."""
    ledger = FundingLedger()
    provider = _StaticRateProvider(0.0001)
    entry_ts = datetime(2026, 5, 1, 23, 59, 0, tzinfo=UTC)
    exit_ts = entry_ts + timedelta(hours=8, minutes=2)
    cost_eth = ledger.compute_funding_cost(
        symbol="ETH-PERP",
        side="LONG",
        qty=1.0,
        entry_price=4000.0,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )
    assert cost_eth > 0  # exactly one 00:00 UTC settlement crossed


def test_callable_provider_signature_works():
    """The ledger should accept a plain ``ts -> float`` callable too,
    not only objects with ``rate_at``."""
    ledger = FundingLedger()
    provider = lambda ts: 0.0001  # noqa: E731 — intentional
    # 24-hour hold from 16:01 UTC to 16:01 UTC the next day strictly
    # contains exactly three settlements: 00:00, 08:00, and 16:00 of
    # the following day. (The 16:00 at entry is *before* entry_ts and
    # the 16:00 at exit IS at exit_ts, which the inclusive-on-exit
    # convention counts.)
    entry_ts = datetime(2026, 5, 1, 16, 1, 0, tzinfo=UTC)
    exit_ts = datetime(2026, 5, 2, 16, 1, 0, tzinfo=UTC)
    cost = ledger.compute_funding_cost(
        symbol="BTC-PERP",
        side="LONG",
        qty=1.0,
        entry_price=60_000.0,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )
    assert cost == pytest.approx(60_000.0 * 0.0001 * 3, abs=1e-6)


def test_zero_qty_returns_zero_cost():
    """Defensive: a zero-qty position should not query the provider."""
    ledger = FundingLedger()
    provider = _StaticRateProvider(0.001)
    entry_ts = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    exit_ts = entry_ts + timedelta(days=2)
    cost = ledger.compute_funding_cost(
        symbol="BTC-PERP",
        side="LONG",
        qty=0.0,
        entry_price=60_000.0,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )
    assert cost == 0.0
    assert provider.calls == []


def test_exit_before_entry_returns_zero():
    """Defensive: malformed window (exit <= entry) returns $0 silently."""
    ledger = FundingLedger()
    provider = _StaticRateProvider(0.001)
    entry_ts = datetime(2026, 5, 2, 0, 0, 0, tzinfo=UTC)
    exit_ts = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)  # before entry
    cost = ledger.compute_funding_cost(
        symbol="BTC-PERP",
        side="LONG",
        qty=1.0,
        entry_price=60_000.0,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )
    assert cost == 0.0


def test_settlement_at_exit_ts_is_included():
    """A settlement that lands exactly on exit_ts is still on the book
    when we close — it gets charged. (Settlement at entry_ts is NOT
    charged because the position is opened just after.)"""
    ledger = FundingLedger()
    rate = 0.0001
    provider = _StaticRateProvider(rate)
    # Entry at 23:59 -> first settlement at 00:00 strictly after entry
    entry_ts = datetime(2026, 5, 1, 23, 59, 0, tzinfo=UTC)
    # Exit exactly at 08:00 -> include 00:00 and 08:00, that's 2 settlements
    exit_ts = datetime(2026, 5, 2, 8, 0, 0, tzinfo=UTC)
    cost = ledger.compute_funding_cost(
        symbol="BTC-PERP",
        side="LONG",
        qty=1.0,
        entry_price=60_000.0,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        funding_provider=provider,
    )
    assert cost == pytest.approx(60_000.0 * rate * 2, abs=1e-6)
