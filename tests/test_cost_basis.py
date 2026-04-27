"""tests.test_cost_basis — FIFO, HIFO, LIFO, partial fills, specific-ID."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eta_engine.tax import AccountTier, CostBasisCalculator, InstrumentType


def _ts(day: int) -> datetime:
    """Return a datetime `day` days after 2025-01-01."""
    from datetime import timedelta

    return datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=day - 1)


class TestFIFO:
    def test_single_lot_single_sell(self) -> None:
        calc = CostBasisCalculator(method="FIFO")
        calc.add_buy("ETH", qty=1.0, price=2000.0, timestamp=_ts(1))
        events = calc.process_sell("ETH", qty=1.0, price=3000.0, timestamp=_ts(100))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_basis_usd == pytest.approx(2000.0)
        assert ev.proceeds_usd == pytest.approx(3000.0)
        assert ev.realized_gain_usd == pytest.approx(1000.0)

    def test_partial_fill_across_lots(self) -> None:
        calc = CostBasisCalculator(method="FIFO")
        calc.add_buy("ETH", qty=1.0, price=2000.0, timestamp=_ts(1))
        calc.add_buy("ETH", qty=2.0, price=2500.0, timestamp=_ts(2))
        events = calc.process_sell("ETH", qty=2.0, price=3000.0, timestamp=_ts(5))
        # FIFO consumes lot1 fully + 1 ETH from lot2
        assert len(events) == 2
        assert events[0].qty == pytest.approx(1.0)
        assert events[0].cost_basis_usd == pytest.approx(2000.0)
        assert events[1].qty == pytest.approx(1.0)
        assert events[1].cost_basis_usd == pytest.approx(2500.0)
        # Remaining lot after sell
        remaining = calc.open_lots("ETH")
        assert len(remaining) == 1
        assert remaining[0].qty == pytest.approx(1.0)

    def test_holding_days_computed(self) -> None:
        calc = CostBasisCalculator()
        calc.add_buy("BTC", qty=1.0, price=40_000.0, timestamp=_ts(1))
        events = calc.process_sell("BTC", qty=1.0, price=50_000.0, timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        assert events[0].holding_days >= 365


class TestHIFODivergence:
    def test_hifo_picks_highest_price_lot_first(self) -> None:
        fifo = CostBasisCalculator(method="FIFO")
        hifo = CostBasisCalculator(method="HIFO")
        for c in (fifo, hifo):
            c.add_buy("SOL", qty=1.0, price=100.0, timestamp=_ts(1))
            c.add_buy("SOL", qty=1.0, price=200.0, timestamp=_ts(2))
        f_events = fifo.process_sell("SOL", qty=1.0, price=250.0, timestamp=_ts(5))
        h_events = hifo.process_sell("SOL", qty=1.0, price=250.0, timestamp=_ts(5))
        # FIFO: cost=100 -> gain=150, HIFO: cost=200 -> gain=50
        assert f_events[0].realized_gain_usd > h_events[0].realized_gain_usd


class TestLIFO:
    def test_lifo_takes_newest_first(self) -> None:
        calc = CostBasisCalculator(method="LIFO")
        calc.add_buy("XRP", qty=100.0, price=0.50, timestamp=_ts(1))
        calc.add_buy("XRP", qty=100.0, price=0.80, timestamp=_ts(3))
        events = calc.process_sell("XRP", qty=50.0, price=1.00, timestamp=_ts(5))
        assert events[0].cost_basis_usd == pytest.approx(50.0 * 0.80)


class TestSpecID:
    def test_specific_id_selection(self) -> None:
        calc = CostBasisCalculator(method="SPEC_ID")
        lot_a = calc.add_buy("ETH", 1.0, 1000.0, _ts(1))
        _ = calc.add_buy("ETH", 1.0, 2000.0, _ts(2))
        events = calc.process_sell("ETH", qty=1.0, price=2500.0, timestamp=_ts(5), lot_id=lot_a.lot_id)
        assert events[0].cost_basis_usd == pytest.approx(1000.0)


class TestTierIsolation:
    def test_us_and_offshore_books_independent(self) -> None:
        calc = CostBasisCalculator()
        calc.add_buy("ETH", 1.0, 2000.0, _ts(1), account_tier=AccountTier.US)
        calc.add_buy("ETH", 1.0, 2500.0, _ts(1), account_tier=AccountTier.OFFSHORE)
        us_events = calc.process_sell(
            "ETH", 1.0, 3000.0, _ts(5), account_tier=AccountTier.US, instrument_type=InstrumentType.CRYPTO_SPOT
        )
        assert us_events[0].cost_basis_usd == pytest.approx(2000.0)
        # Offshore book untouched
        assert calc.open_lots("ETH", AccountTier.OFFSHORE)[0].qty == 1.0


class TestErrors:
    def test_oversell_raises(self) -> None:
        calc = CostBasisCalculator()
        calc.add_buy("ETH", 1.0, 2000.0, _ts(1))
        with pytest.raises(ValueError):
            calc.process_sell("ETH", qty=2.0, price=3000.0, timestamp=_ts(5))
