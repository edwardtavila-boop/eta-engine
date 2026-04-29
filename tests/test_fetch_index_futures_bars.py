from __future__ import annotations

import sys

from eta_engine.scripts import fetch_index_futures_bars as mod


def test_resample_4h_rolls_up_ohlcv() -> None:
    rows = [
        {"time": 1_735_689_600, "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 10.0},
        {"time": 1_735_693_200, "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5, "volume": 20.0},
        {"time": 1_735_696_800, "open": 101.5, "high": 103.0, "low": 101.0, "close": 102.5, "volume": 30.0},
        {"time": 1_735_700_400, "open": 102.5, "high": 104.0, "low": 102.0, "close": 103.5, "volume": 40.0},
        {"time": 1_735_704_000, "open": 103.5, "high": 105.0, "low": 103.0, "close": 104.5, "volume": 50.0},
    ]

    out = mod._resample_4h(rows)

    assert len(out) == 2
    assert out[0] == {
        "time": 1_735_689_600,
        "open": 100.0,
        "high": 104.0,
        "low": 99.5,
        "close": 103.5,
        "volume": 100.0,
    }
    assert out[1] == {
        "time": 1_735_704_000,
        "open": 103.5,
        "high": 105.0,
        "low": 103.0,
        "close": 104.5,
        "volume": 50.0,
    }


def test_four_hour_timeframe_uses_hourly_yfinance_source(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeFrame:
        def __len__(self) -> int:
            return 0

    class FakeTicker:
        def __init__(self, ticker: str) -> None:
            self.ticker = ticker

        def history(self, *, period: str, interval: str) -> FakeFrame:
            calls.append((period, interval))
            return FakeFrame()

    class FakeYFinance:
        Ticker = FakeTicker

    monkeypatch.setitem(sys.modules, "yfinance", FakeYFinance)

    assert mod._fetch_via_yfinance("NQ", "4h", "730d") == []
    assert calls == [("730d", "1h")]
