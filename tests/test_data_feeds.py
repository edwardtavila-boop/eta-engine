"""Tests for the supervisor's real-data feeds.

Each feed must satisfy the bar-shape contract that the supervisor relies on
(open/high/low/close/volume/ts/symbol) and degrade safely on transient
errors so the supervisor's tick loop never crashes mid-fleet.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _assert_bar_shape(bar: dict, symbol: str) -> None:
    for key in ("open", "high", "low", "close", "volume", "ts", "symbol"):
        assert key in bar, f"missing key {key} in bar {bar}"
    assert bar["symbol"] == symbol


# ─── Symbol-type routing helpers ─────────────────────────────────


def test_root_strips_contract_month_and_currency_suffix() -> None:
    from eta_engine.scripts.data_feeds import _root
    assert _root("MNQ1") == "MNQ"
    assert _root("NQ1") == "NQ"
    assert _root("BTC") == "BTC"
    assert _root("BTCUSD") == "BTC"
    assert _root("ETHUSDT") == "ETH"
    assert _root("/MNQ") == "MNQ"


def test_is_crypto_and_futures_classification() -> None:
    from eta_engine.scripts.data_feeds import _is_crypto, _is_futures
    assert _is_crypto("BTC")
    assert _is_crypto("BTCUSD")
    assert _is_crypto("ETH")
    assert _is_crypto("DOGE")
    assert not _is_crypto("MNQ")

    assert _is_futures("MNQ1")
    assert _is_futures("NQ")
    assert _is_futures("ES1")
    assert not _is_futures("BTC")
    # MBT/MET are micro-crypto futures — treated as crypto for routing
    assert not _is_futures("MBT")


# ─── Empty-bar fallback ─────────────────────────────────────────


def test_empty_bar_has_full_shape() -> None:
    from eta_engine.scripts.data_feeds import _empty_bar
    bar = _empty_bar("BTC", last_close=95000.0)
    _assert_bar_shape(bar, "BTC")
    assert bar["close"] == 95000.0
    assert bar["volume"] == 0.0


# ─── YFinanceDataFeed ─────────────────────────────────────────


def test_yfinance_feed_caches_within_ttl() -> None:
    """Two get_bar calls within the TTL window MUST hit the cache,
    not the network."""
    from eta_engine.scripts.data_feeds import YFinanceDataFeed
    feed = YFinanceDataFeed(ttl_seconds=30.0)

    with patch("yfinance.Ticker") as mock_ticker:
        mock_hist = MagicMock()
        mock_hist.empty = False
        mock_hist.iloc = [_FakeRow()]  # type: ignore[assignment]
        mock_ticker.return_value.history.return_value = mock_hist

        bar1 = feed.get_bar("BTC")
        bar2 = feed.get_bar("BTC")

    _assert_bar_shape(bar1, "BTC")
    _assert_bar_shape(bar2, "BTC")
    # Only one upstream call despite two get_bar calls
    assert mock_ticker.return_value.history.call_count == 1


def test_yfinance_feed_returns_empty_bar_on_unknown_symbol() -> None:
    from eta_engine.scripts.data_feeds import YFinanceDataFeed
    feed = YFinanceDataFeed()
    bar = feed.get_bar("FAKECOIN")
    _assert_bar_shape(bar, "FAKECOIN")
    # Unknown symbol short-circuits to fallback bar
    assert bar["volume"] == 0.0


def test_yfinance_feed_survives_network_failure() -> None:
    from eta_engine.scripts.data_feeds import YFinanceDataFeed
    feed = YFinanceDataFeed()
    with patch("yfinance.Ticker", side_effect=RuntimeError("net down")):
        bar = feed.get_bar("BTC")
    _assert_bar_shape(bar, "BTC")
    assert bar["volume"] == 0.0  # fallback


class _FakeRow:
    """Stand-in for a yfinance DataFrame row."""
    def __getitem__(self, key: str) -> float:
        return {
            "Open": 95000.0, "High": 95100.0, "Low": 94900.0,
            "Close": 95050.0, "Volume": 1234.0,
        }[key]

    def get(self, key: str, default: float = 0.0) -> float:
        return self[key]


# ─── CoinbaseDataFeed ────────────────────────────────────────────


def test_coinbase_feed_pulls_latest_candle() -> None:
    from eta_engine.scripts.data_feeds import CoinbaseDataFeed
    feed = CoinbaseDataFeed(ttl_seconds=30.0)

    fake_resp = MagicMock()
    fake_resp.json.return_value = [
        # most recent: [time, low, high, open, close, volume]
        [1730750400, 94900.0, 95100.0, 95000.0, 95050.0, 12.5],
        [1730750340, 94800.0, 95000.0, 94900.0, 94950.0, 10.1],
    ]
    fake_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=fake_resp) as mock_get:
        bar = feed.get_bar("BTC")
        bar2 = feed.get_bar("BTC")  # cached

    _assert_bar_shape(bar, "BTC")
    assert bar["close"] == 95050.0
    assert bar["volume"] == 12.5
    assert mock_get.call_count == 1, "second call should hit cache"
    _assert_bar_shape(bar2, "BTC")


def test_coinbase_feed_unknown_symbol_returns_empty_bar() -> None:
    from eta_engine.scripts.data_feeds import CoinbaseDataFeed
    feed = CoinbaseDataFeed()
    bar = feed.get_bar("MNQ1")  # not crypto
    _assert_bar_shape(bar, "MNQ1")
    assert bar["volume"] == 0.0


def test_coinbase_feed_survives_http_failure() -> None:
    from eta_engine.scripts.data_feeds import CoinbaseDataFeed
    feed = CoinbaseDataFeed()
    with patch("requests.get", side_effect=RuntimeError("502")):
        bar = feed.get_bar("BTC")
    _assert_bar_shape(bar, "BTC")
    assert bar["volume"] == 0.0


# ─── IbkrDataFeed ────────────────────────────────────────────────


def test_ibkr_feed_uses_historical_data_for_futures() -> None:
    from eta_engine.scripts.data_feeds import IbkrDataFeed
    feed = IbkrDataFeed()
    # Mock the underlying IB and the connection
    fake_ib = MagicMock()
    fake_ib.isConnected.return_value = True
    fake_bar = MagicMock(open=21500.0, high=21520.0, low=21480.0, close=21510.0, volume=1500)
    fake_ib.reqHistoricalData.return_value = [fake_bar]
    feed._ib = fake_ib

    bar = feed.get_bar("MNQ1")
    _assert_bar_shape(bar, "MNQ1")
    assert bar["close"] == 21510.0
    fake_ib.reqHistoricalData.assert_called_once()


def test_ibkr_feed_returns_empty_when_disconnected() -> None:
    from eta_engine.scripts.data_feeds import IbkrDataFeed
    feed = IbkrDataFeed()
    feed._ib = MagicMock()
    feed._ib.isConnected.return_value = False
    # Patch _ensure_connected to keep returning False so we hit the
    # empty-bar fallback without trying to actually connect to TWS.
    feed._ensure_connected = MagicMock(return_value=False)  # type: ignore[method-assign]
    bar = feed.get_bar("MNQ1")
    _assert_bar_shape(bar, "MNQ1")
    assert bar["volume"] == 0.0


def test_ibkr_feed_unknown_symbol_returns_empty_bar() -> None:
    from eta_engine.scripts.data_feeds import IbkrDataFeed
    feed = IbkrDataFeed()
    feed._ib = MagicMock()
    feed._ib.isConnected.return_value = True
    feed._ensure_connected = MagicMock(return_value=True)  # type: ignore[method-assign]
    bar = feed.get_bar("FAKECONTRACT")
    _assert_bar_shape(bar, "FAKECONTRACT")
    assert bar["volume"] == 0.0


# ─── CompositeDataFeed ───────────────────────────────────────────


def test_composite_routes_crypto_to_coinbase_and_futures_to_ibkr() -> None:
    from eta_engine.scripts.data_feeds import CompositeDataFeed
    feed = CompositeDataFeed()

    fake_coinbase = MagicMock()
    fake_coinbase.get_bar.return_value = {
        "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
        "ts": "x", "symbol": "BTC",
    }
    fake_ibkr = MagicMock()
    fake_ibkr.get_bar.return_value = {
        "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2,
        "ts": "x", "symbol": "MNQ1",
    }
    fake_yf = MagicMock()
    fake_yf.get_bar.return_value = {
        "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3,
        "ts": "x", "symbol": "AAPL",
    }
    feed._coinbase = fake_coinbase
    feed._ibkr = fake_ibkr
    feed._yf = fake_yf

    feed.get_bar("BTC")
    feed.get_bar("MNQ1")
    feed.get_bar("AAPL")

    fake_coinbase.get_bar.assert_called_once_with("BTC")
    fake_ibkr.get_bar.assert_called_once_with("MNQ1")
    fake_yf.get_bar.assert_called_once_with("AAPL")


# ─── Factory ─────────────────────────────────────────────────────


def test_factory_returns_correct_class_per_name() -> None:
    from eta_engine.scripts.data_feeds import (
        CoinbaseDataFeed,
        CompositeDataFeed,
        IbkrDataFeed,
        YFinanceDataFeed,
        make_data_feed,
    )
    assert isinstance(make_data_feed("yfinance"), YFinanceDataFeed)
    assert isinstance(make_data_feed("coinbase"), CoinbaseDataFeed)
    assert isinstance(make_data_feed("ibkr"), IbkrDataFeed)
    assert isinstance(make_data_feed("composite"), CompositeDataFeed)


def test_factory_falls_back_to_mock_for_unknown_name() -> None:
    from eta_engine.scripts.data_feeds import make_data_feed
    from eta_engine.scripts.jarvis_strategy_supervisor import MockDataFeed
    assert isinstance(make_data_feed("typo-name"), MockDataFeed)
    assert isinstance(make_data_feed(""), MockDataFeed)
