"""
EVOLUTIONARY TRADING ALGO  //  tax.koinly_exporter
======================================
Export TaxableEvent list to Koinly Universal CSV format.
Reference: https://help.koinly.io/en/articles/3662999-how-to-create-a-universal-csv
"""

from __future__ import annotations

import csv
from pathlib import Path

from eta_engine.tax.models import EventType, TaxableEvent

# Koinly universal CSV label vocabulary
_KOINLY_LABELS = {
    EventType.TRADE_CLOSE: "trade",
    EventType.STAKING_RECEIPT: "staking",
    EventType.AIRDROP: "airdrop",
    EventType.FEE: "cost",
    EventType.FUNDING_PAYMENT: "income",
    EventType.TRANSFER: "transfer",
}

# Full set of Koinly-recognized transaction labels (documented)
KOINLY_LABEL_VOCAB: tuple[str, ...] = (
    "trade",
    "send",
    "receive",
    "staking",
    "mining",
    "fork",
    "airdrop",
    "gift",
    "lost",
    "reward",
    "interest",
    "cost",
    "income",
    "transfer",
)

_COLUMNS: list[str] = [
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


class KoinlyExporter:
    """Write Koinly universal CSV for a list of TaxableEvent rows."""

    def __init__(self, fee_currency: str = "USD") -> None:
        self.fee_currency = fee_currency

    def export_csv(
        self,
        events: list[TaxableEvent],
        path: Path,
    ) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Koinly expects chronological order
        sorted_events = sorted(events, key=lambda e: e.timestamp)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            writer.writeheader()
            for ev in sorted_events:
                writer.writerow(self._row_for(ev))
        return path

    def _row_for(self, ev: TaxableEvent) -> dict[str, str]:
        label = _KOINLY_LABELS.get(ev.event_type, "trade")
        date_str = ev.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        desc = f"{ev.instrument_type.value} / {ev.account_tier.value}"
        row: dict[str, str] = {c: "" for c in _COLUMNS}
        row["Date"] = date_str
        row["Label"] = label
        row["Description"] = desc
        row["TxHash"] = ev.event_id
        row["Net Worth Amount"] = f"{ev.proceeds_usd:.6f}"
        row["Net Worth Currency"] = "USD"

        if ev.event_type == EventType.TRADE_CLOSE:
            # A close: we sent `asset` qty, received USD `proceeds`
            row["Sent Amount"] = f"{ev.qty:.8f}"
            row["Sent Currency"] = ev.asset
            row["Received Amount"] = f"{max(ev.proceeds_usd, 0.0):.6f}"
            row["Received Currency"] = "USD"
        elif ev.event_type in (
            EventType.STAKING_RECEIPT,
            EventType.AIRDROP,
            EventType.FUNDING_PAYMENT,
        ):
            # Income side: received asset
            row["Received Amount"] = f"{ev.qty:.8f}"
            row["Received Currency"] = ev.asset
        elif ev.event_type == EventType.FEE:
            row["Fee Amount"] = f"{ev.qty:.8f}"
            row["Fee Currency"] = ev.asset if ev.qty > 0 else self.fee_currency
        elif ev.event_type == EventType.TRANSFER:
            row["Sent Amount"] = f"{ev.qty:.8f}"
            row["Sent Currency"] = ev.asset
            row["Received Amount"] = f"{ev.qty:.8f}"
            row["Received Currency"] = ev.asset
        return row

    @staticmethod
    def known_labels() -> tuple[str, ...]:
        return KOINLY_LABEL_VOCAB
