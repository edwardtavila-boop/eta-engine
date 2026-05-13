"""Tests for ``eta_engine.data.tradingview.parsers`` -- pure parsers."""

from __future__ import annotations

import json

from eta_engine.data.tradingview.parsers import (
    parse_alert_row,
    parse_indicator_tooltip,
    parse_quote_frame,
    parse_watchlist_row,
)

# ---------------------------------------------------------------------------
# parse_quote_frame
# ---------------------------------------------------------------------------


def _frame(payload_json: str) -> str:
    """Wrap ``payload_json`` in TradingView's ``~m~<len>~m~`` framing."""
    return f"~m~{len(payload_json)}~m~{payload_json}"


def test_parse_quote_frame_qsd_tick() -> None:
    body = json.dumps(
        {
            "m": "qsd",
            "p": [
                "sess1",
                {
                    "n": "BINANCE:BTCUSDT",
                    "s": "ok",
                    "v": {"lp": 50_000.5, "lp_time": 1_714_000_000},
                },
            ],
        }
    )
    out = parse_quote_frame(_frame(body))
    assert len(out) == 1
    assert out[0]["kind"] == "tick"
    assert out[0]["symbol"] == "BINANCE:BTCUSDT"
    assert out[0]["price"] == 50_000.5
    assert out[0]["ts"] == 1_714_000_000.0


def test_parse_quote_frame_timescale_update_bar() -> None:
    body = json.dumps(
        {
            "m": "timescale_update",
            "p": [
                "chart1",
                {
                    "sds_1": {
                        "ns": {"d": "BINANCE:BTCUSDT"},
                        "s": [{"i": 0, "v": [1_714_000_000, 50000, 50100, 49900, 50050, 12.5]}],
                    },
                },
            ],
        }
    )
    out = parse_quote_frame(_frame(body))
    assert len(out) == 1
    bar = out[0]
    assert bar["kind"] == "bar"
    assert bar["symbol"] == "BINANCE:BTCUSDT"
    assert bar["o"] == 50000.0
    assert bar["c"] == 50050.0
    assert bar["v"] == 12.5


def test_parse_quote_frame_handles_concatenated_frames() -> None:
    a = json.dumps({"m": "qsd", "p": ["s", {"n": "X:Y", "v": {"lp": 1.0}}]})
    b = json.dumps({"m": "qsd", "p": ["s", {"n": "X:Y2", "v": {"lp": 2.0}}]})
    payload = _frame(a) + _frame(b)
    out = parse_quote_frame(payload)
    assert {r["symbol"] for r in out} == {"X:Y", "X:Y2"}


def test_parse_quote_frame_ignores_heartbeat() -> None:
    assert parse_quote_frame("~h~12") == []
    assert parse_quote_frame("") == []
    assert parse_quote_frame("garbage") == []


def test_parse_quote_frame_ignores_bad_json() -> None:
    payload = "~m~5~m~not{j"
    assert parse_quote_frame(payload) == []


def test_parse_quote_frame_handles_bytes() -> None:
    body = json.dumps({"m": "qsd", "p": ["s", {"n": "A:B", "v": {"lp": 5.0}}]})
    out = parse_quote_frame(_frame(body).encode("utf-8"))
    assert out and out[0]["price"] == 5.0


# ---------------------------------------------------------------------------
# parse_indicator_tooltip
# ---------------------------------------------------------------------------


def test_parse_indicator_tooltip_simple_rsi() -> None:
    out = parse_indicator_tooltip("RSI (14, close)  62.43")
    assert out is not None
    assert out["indicator"] == "RSI"
    assert out["params"] == "14, close"
    assert out["value"] == 62.43
    assert out["all"] == [62.43]


def test_parse_indicator_tooltip_macd_multivalue() -> None:
    out = parse_indicator_tooltip("MACD (12, 26, 9)  0.45  0.32  0.13")
    assert out is not None
    assert out["indicator"] == "MACD"
    assert out["value"] == 0.45
    assert out["all"] == [0.45, 0.32, 0.13]


def test_parse_indicator_tooltip_volume_ma_with_si() -> None:
    out = parse_indicator_tooltip("Volume MA  120.5K")
    assert out is not None
    assert out["indicator"] == "Volume MA"
    assert out["value"] == 120_500.0


def test_parse_indicator_tooltip_returns_none_on_garbage() -> None:
    assert parse_indicator_tooltip("") is None
    assert parse_indicator_tooltip("just words no numbers") is None
    assert parse_indicator_tooltip(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_watchlist_row
# ---------------------------------------------------------------------------


def test_parse_watchlist_row_basic() -> None:
    row = parse_watchlist_row(
        {
            "symbol": "BINANCE:BTCUSDT",
            "last": "50,123.4",
            "chg": "+1.42%",
            "vol": "1.2B",
        }
    )
    assert row is not None
    assert row["symbol"] == "BINANCE:BTCUSDT"
    assert row["last"] == 50_123.4
    assert abs(row["chg_pct"] - 1.42) < 1e-9
    assert row["vol"] == 1.2e9


def test_parse_watchlist_row_missing_symbol_returns_none() -> None:
    assert parse_watchlist_row({"last": "1"}) is None


def test_parse_watchlist_row_handles_empty_fields() -> None:
    row = parse_watchlist_row({"symbol": "X:Y", "last": "", "chg": "", "vol": ""})
    assert row == {"symbol": "X:Y", "last": 0.0, "chg_pct": 0.0, "vol": 0.0}


# ---------------------------------------------------------------------------
# parse_alert_row
# ---------------------------------------------------------------------------


def test_parse_alert_row_definition() -> None:
    out = parse_alert_row(
        {
            "name": "BTCUSDT > 60000",
            "symbol": "BINANCE:BTCUSDT",
            "condition": ">",
            "value": "60000",
            "active": True,
        }
    )
    assert out is not None
    assert out["symbol"] == "BINANCE:BTCUSDT"
    assert out["condition"] == ">"
    assert out["value"] == 60_000.0
    assert out["active"] is True
    assert out["fired_at"] is None


def test_parse_alert_row_fired() -> None:
    out = parse_alert_row(
        {
            "name": "ETH cross",
            "symbol": "BINANCE:ETHUSDT",
            "condition": "==",
            "value": "3000",
            "active": False,
            "fired_at": "2026-04-20T12:00:00Z",
        }
    )
    assert out is not None
    assert out["active"] is False
    assert out["fired_at"] == "2026-04-20T12:00:00Z"


def test_parse_alert_row_missing_essential() -> None:
    assert parse_alert_row({}) is None
    assert parse_alert_row({"name": "", "symbol": ""}) is None
    assert parse_alert_row("not-a-dict") is None  # type: ignore[arg-type]
