from __future__ import annotations

import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

from eta_engine.scripts import fetch_market_context_bars as mod


class _FakeTimestamp:
    tzinfo = UTC

    def __init__(self, ts: datetime) -> None:
        self.ts = ts

    def tz_convert(self, _tz: object) -> datetime:
        return self.ts


def test_output_timeframe_normalizes_daily_aliases() -> None:
    assert mod._output_timeframe("D") == "D"
    assert mod._output_timeframe("1d") == "D"
    assert mod._output_timeframe("5m") == "5m"


def test_fetch_via_yfinance_uses_context_symbol_mapping(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []
    ts = datetime(2026, 4, 29, 19, 45, tzinfo=UTC)

    class FakeFrame:
        def __len__(self) -> int:
            return 1

        def iterrows(self):
            yield (
                _FakeTimestamp(ts),
                {
                    "Open": 10.0,
                    "High": 11.0,
                    "Low": 9.5,
                    "Close": 10.5,
                    "Volume": 123.0,
                },
            )

    class FakeTicker:
        def __init__(self, ticker: str) -> None:
            self.ticker = ticker

        def history(self, *, period: str, interval: str) -> FakeFrame:
            calls.append((self.ticker, period, interval))
            return FakeFrame()

    class FakeYFinance:
        Ticker = FakeTicker

    monkeypatch.setitem(sys.modules, "yfinance", FakeYFinance)

    rows = mod._fetch_via_yfinance("VIX", "1m", "7d")

    assert calls == [("^VIX", "7d", "1m")]
    assert rows == [
        {
            "time": int(ts.timestamp()),
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": 10.5,
            "volume": 123.0,
        }
    ]


def test_merge_with_existing_keeps_unique_sorted_rows(tmp_path: Path) -> None:
    path = tmp_path / "DXY_5m.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        writer.writerow([20, 2.0, 3.0, 1.0, 2.5, 200.0])

    merged, existing, new = mod._merge_with_existing(
        path,
        [
            {"time": 20, "open": 2.0, "high": 3.0, "low": 1.0, "close": 2.5, "volume": 200.0},
            {"time": 10, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0},
        ],
    )

    assert existing == 1
    assert new == 1
    assert [row["time"] for row in merged] == [10, 20]
