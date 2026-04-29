from __future__ import annotations

import csv
from typing import TYPE_CHECKING

from eta_engine.scripts.hydrate_canonical_market_data import (
    _canonical_history_name_from_databento,
    _canonical_history_name_from_main,
    _convert_main_to_history,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_canonical_history_name_from_databento_normalizes_legacy_names() -> None:
    assert _canonical_history_name_from_databento("mnq1_5m.csv") == "MNQ1_5m.csv"
    assert _canonical_history_name_from_databento("nq_1m.csv") == "NQ1_1m.csv"
    assert _canonical_history_name_from_databento("es_5m.csv") == "ES1_5m.csv"
    assert _canonical_history_name_from_databento("vix_yf_d.csv") == "VIX_D.csv"


def test_canonical_history_name_from_main_promotes_root_futures_names() -> None:
    assert _canonical_history_name_from_main("mnq_5m.csv") == "MNQ1_5m.csv"
    assert _canonical_history_name_from_main("mnq_es1_5.csv") == "ES1_5m.csv"
    assert _canonical_history_name_from_main("nq_D.csv") == "NQ1_D.csv"


def test_convert_main_to_history_rewrites_schema(tmp_path: Path) -> None:
    source = tmp_path / "nq_D.csv"
    target = tmp_path / "history" / "NQ1_D.csv"
    with source.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["timestamp_utc", "epoch_s", "open", "high", "low", "close", "volume", "session"],
        )
        writer.writerow(["2026-01-01T00:00:00Z", 1735689600, 100.0, 101.0, 99.0, 100.5, 1000, "ETH"])
        writer.writerow(["2026-01-02T00:00:00Z", 1735776000, 100.5, 102.0, 100.0, 101.5, 1200, "ETH"])

    rows = _convert_main_to_history(source, target)

    assert rows == 2
    assert target.exists()
    with target.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        materialized = list(reader)
    assert reader.fieldnames == ["time", "open", "high", "low", "close", "volume"]
    assert materialized[0]["time"] == "1735689600"
    assert materialized[1]["close"] == "101.5"
