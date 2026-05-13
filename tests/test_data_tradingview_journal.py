"""Tests for ``eta_engine.data.tradingview.journal``.

Covers the four stream persisters (bars / indicators / watchlist /
alerts), exit-paths on OSError, and atomic-replace behavior on the
watchlist snapshot.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path  # noqa: TC003 -- used at runtime via tmp_path

from eta_engine.data.tradingview.journal import (
    AlertEntry,
    BarEntry,
    IndicatorEntry,
    TradingViewJournal,
    WatchlistSnapshot,
    now_iso,
)


def test_record_bar_appends_gzipped_line(tmp_path: Path) -> None:
    j = TradingViewJournal(tmp_path)
    j.record_bar(
        BarEntry(
            ts=1_714_000_000.0,
            symbol="BINANCE:BTCUSDT",
            interval="1",
            o=50_000.0,
            h=50_100.0,
            l=49_900.0,
            c=50_050.0,
            v=12.5,
        )
    )
    sym_dir = tmp_path / "bars" / "BINANCE_BTCUSDT"
    files = list(sym_dir.glob("*.jsonl.gz"))
    assert len(files) == 1
    with gzip.open(files[0], "rt") as f:
        line = f.readline().strip()
    parsed = json.loads(line)
    assert parsed["symbol"] == "BINANCE:BTCUSDT"
    assert parsed["c"] == 50_050.0


def test_record_bar_two_appends_into_same_day_file(tmp_path: Path) -> None:
    j = TradingViewJournal(tmp_path)
    e = BarEntry(
        ts=1_714_000_000.0,
        symbol="A:B",
        interval="1",
        o=1.0,
        h=2.0,
        l=0.5,
        c=1.5,
        v=10.0,
    )
    j.record_bar(e)
    j.record_bar(e)
    files = list((tmp_path / "bars" / "A_B").glob("*.jsonl.gz"))
    assert len(files) == 1
    with gzip.open(files[0], "rt") as f:
        lines = f.readlines()
    assert len(lines) == 2


def test_record_indicator_appends_jsonl(tmp_path: Path) -> None:
    j = TradingViewJournal(tmp_path)
    j.record_indicator(
        IndicatorEntry(
            ts=now_iso(),
            symbol="X:Y",
            interval="5",
            indicator="RSI",
            params="14, close",
            value=62.4,
            all=[62.4],
        )
    )
    j.record_indicator(
        IndicatorEntry(
            ts=now_iso(),
            symbol="X:Y",
            interval="5",
            indicator="MACD",
            params="12, 26, 9",
            value=0.4,
            all=[0.4, 0.3, 0.1],
        )
    )
    rows = (tmp_path / "indicators.jsonl").read_text().splitlines()
    assert len(rows) == 2
    parsed = [json.loads(r) for r in rows]
    assert parsed[0]["indicator"] == "RSI"
    assert parsed[1]["all"] == [0.4, 0.3, 0.1]


def test_record_alert_appends_jsonl(tmp_path: Path) -> None:
    j = TradingViewJournal(tmp_path)
    j.record_alert(
        AlertEntry(
            ts=now_iso(),
            kind="definition",
            symbol="BINANCE:BTCUSDT",
            name="BTC > 60k",
            condition=">",
            value=60_000.0,
            active=True,
            fired_at=None,
        )
    )
    rows = (tmp_path / "alerts.jsonl").read_text().splitlines()
    assert len(rows) == 1
    p = json.loads(rows[0])
    assert p["symbol"] == "BINANCE:BTCUSDT"
    assert p["kind"] == "definition"


def test_record_watchlist_overwrites_atomically(tmp_path: Path) -> None:
    j = TradingViewJournal(tmp_path)
    snap1 = WatchlistSnapshot(ts=now_iso(), lists={"default": [{"symbol": "A:B"}]})
    snap2 = WatchlistSnapshot(ts=now_iso(), lists={"default": [{"symbol": "C:D"}]})
    j.record_watchlist(snap1)
    j.record_watchlist(snap2)
    payload = json.loads((tmp_path / "watchlist.json").read_text())
    assert payload["lists"]["default"][0]["symbol"] == "C:D"


def test_journal_swallows_oserror_on_bar(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    j = TradingViewJournal(tmp_path)

    def _raise(*_a, **_k) -> None:
        raise OSError("boom")

    monkeypatch.setattr("gzip.open", _raise)
    # Should NOT raise -- best-effort persistence.
    j.record_bar(
        BarEntry(
            ts=1.0,
            symbol="X:Y",
            interval="1",
            o=1.0,
            h=1.0,
            l=1.0,
            c=1.0,
            v=0.0,
        )
    )


def test_record_bar_uses_now_when_ts_zero(tmp_path: Path) -> None:
    j = TradingViewJournal(tmp_path)
    j.record_bar(
        BarEntry(
            ts=0.0,
            symbol="X:Y",
            interval="1",
            o=1.0,
            h=1.0,
            l=1.0,
            c=1.0,
            v=0.0,
        )
    )
    files = list((tmp_path / "bars" / "X_Y").glob("*.jsonl.gz"))
    assert len(files) == 1


def test_now_iso_format() -> None:
    s = now_iso()
    # 2026-04-27T12:00:00.123456Z  -> 27 chars total + Z
    assert s.endswith("Z")
    assert "T" in s
