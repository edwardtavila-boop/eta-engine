"""tests.test_tax_reporter — Section 1256 60/40, Koinly CSV, Form 6781."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from eta_engine.tax import (
    AccountTier,
    EventType,
    InstrumentType,
    KoinlyExporter,
    OpenFuturesPosition,
    Section1256Reporter,
    TaxableEvent,
)

if TYPE_CHECKING:
    from pathlib import Path


def _ev(
    asset: str = "ETH",
    gain: float = 100.0,
    qty: float = 1.0,
    instr: InstrumentType = InstrumentType.CRYPTO_SPOT,
    evt: EventType = EventType.TRADE_CLOSE,
    day: int = 1,
) -> TaxableEvent:
    return TaxableEvent(
        event_id=f"e-{day}-{asset}",
        timestamp=datetime(2025, 1, day, tzinfo=UTC),
        event_type=evt,
        asset=asset,
        qty=qty,
        cost_basis_usd=1000.0,
        proceeds_usd=1000.0 + gain,
        realized_gain_usd=gain,
        holding_days=30,
        account_tier=AccountTier.US,
        instrument_type=instr,
    )


class TestSection1256:
    def test_60_40_split(self) -> None:
        r = Section1256Reporter()
        out = r.breakdown_60_40(10_000.0)
        assert out["long_term_60"] == pytest.approx(6000.0)
        assert out["short_term_40"] == pytest.approx(4000.0)
        assert out["total"] == pytest.approx(10_000.0)

    def test_60_40_negative_loss(self) -> None:
        r = Section1256Reporter()
        out = r.breakdown_60_40(-5000.0)
        assert out["long_term_60"] == pytest.approx(-3000.0)
        assert out["short_term_40"] == pytest.approx(-2000.0)

    def test_mark_to_market_creates_events(self) -> None:
        r = Section1256Reporter()
        pos = OpenFuturesPosition(
            symbol="MNQ",
            qty=5.0,
            entry_price=20_000.0,
            entry_time=datetime(2025, 6, 1, tzinfo=UTC),
        )
        events = r.mark_to_market(
            [pos],
            year_end_date=datetime(2025, 12, 31, tzinfo=UTC),
            year_end_prices={"MNQ": 21_000.0},
        )
        assert len(events) == 1
        assert events[0].realized_gain_usd == pytest.approx(5000.0)
        assert events[0].instrument_type == InstrumentType.FUTURES_1256

    def test_form_6781_summary(self) -> None:
        r = Section1256Reporter()
        events = [
            _ev("MNQ", gain=1000.0, instr=InstrumentType.FUTURES_1256, day=2),
            _ev("NQ", gain=500.0, instr=InstrumentType.FUTURES_1256, day=3),
            _ev("ETH", gain=300.0, instr=InstrumentType.CRYPTO_SPOT, day=4),  # excluded
        ]
        summary = r.generate_form_6781_summary(events)
        assert summary["n_contracts"] == 2
        assert summary["line_2_sum"] == pytest.approx(1500.0)
        assert summary["line_8_short_term_40pct"] == pytest.approx(600.0)
        assert summary["line_9_long_term_60pct"] == pytest.approx(900.0)


class TestKoinlyExport:
    def test_csv_header_matches_spec(self, tmp_path: Path) -> None:
        exp = KoinlyExporter()
        path = tmp_path / "koinly.csv"
        exp.export_csv([_ev()], path)
        with open(path, encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
        assert header == [
            "Date",
            "Sent Amount",
            "Sent Currency",
            "Received Amount",
            "Received Currency",
            "Fee Amount",
            "Fee Currency",
            "Net Worth Amount",
            "Net Worth Currency",
            "Label",
            "Description",
            "TxHash",
        ]

    def test_trade_row_shape(self, tmp_path: Path) -> None:
        exp = KoinlyExporter()
        path = tmp_path / "k.csv"
        exp.export_csv([_ev("ETH", gain=100.0, qty=1.5)], path)
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["Sent Currency"] == "ETH"
        assert rows[0]["Sent Amount"] == "1.50000000"
        assert rows[0]["Label"] == "trade"

    def test_staking_row_uses_received(self, tmp_path: Path) -> None:
        exp = KoinlyExporter()
        path = tmp_path / "k2.csv"
        stk = _ev("SOL", gain=0.0, qty=0.1, evt=EventType.STAKING_RECEIPT)
        exp.export_csv([stk], path)
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        assert rows[0]["Label"] == "staking"
        assert rows[0]["Received Currency"] == "SOL"

    def test_known_labels_vocabulary(self) -> None:
        labels = KoinlyExporter.known_labels()
        for expected in ("trade", "staking", "airdrop", "gift", "lost"):
            assert expected in labels
