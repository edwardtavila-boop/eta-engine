"""Tests for the supervisor's real-data feeds.

Each feed must satisfy the bar-shape contract that the supervisor relies on
(open/high/low/close/volume/ts/symbol) and degrade safely on transient
errors so the supervisor's tick loop never crashes mid-fleet.
"""
from __future__ import annotations

import builtins
import sys
import threading
import time
import types
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
    # Mock the underlying IB and the connection. _make_contract calls
    # _resolve_front_month_mnq which itself calls qualifyContracts then
    # reads lastTradeDateOrContractMonth — provide an explicit string
    # so the YYYYMM regex parser succeeds (the bare MagicMock proxies
    # any attribute access to another mock object that fails parsing).
    fake_ib = MagicMock()
    fake_ib.isConnected.return_value = True
    qualified_contract = MagicMock()
    qualified_contract.lastTradeDateOrContractMonth = "20260620"
    fake_ib.qualifyContracts.return_value = [qualified_contract]
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


def test_ibkr_feed_constructor_does_not_import_ib_insync() -> None:
    """The IBKR feed must stay import-light until a real connection is requested."""
    from eta_engine.scripts.data_feeds import IbkrDataFeed, make_data_feed

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "ib_insync":
            raise AssertionError("IbkrDataFeed imported ib_insync before _ensure_connected")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=guarded_import):
        feed = IbkrDataFeed()
        factory_feed = make_data_feed("ibkr")

    assert feed._ib is None
    assert factory_feed._ib is None


def test_ibkr_feed_lazy_connect_is_mid_session_thread_safe(monkeypatch) -> None:
    """Concurrent mid-session consumers should share one lazy IB connection.

    This exercises the path the live supervisor uses when several futures bots
    ask for a bar on the same tick. It uses a fake ``ib_insync`` module, never a
    live IBKR/TWS account.
    """
    from eta_engine.scripts.data_feeds import IbkrDataFeed

    fake_module = types.ModuleType("ib_insync")
    lock = threading.Lock()
    connects: list[tuple[str, int, int, int]] = []
    market_data_types: list[int] = []

    class FakeIB:
        def __init__(self) -> None:
            self.connected = False

        def isConnected(self) -> bool:  # noqa: N802 - mirrors ib_insync.IB
            return self.connected

        def connect(self, host: str, port: int, *, clientId: int, timeout: int) -> None:  # noqa: N803
            with lock:
                connects.append((host, port, clientId, timeout))
            time.sleep(0.02)
            self.connected = True

        def reqMarketDataType(self, market_data_type: int) -> None:  # noqa: N802 - mirrors ib_insync.IB
            with lock:
                market_data_types.append(market_data_type)

    fake_module.IB = FakeIB
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)
    monkeypatch.setenv("ETA_IBKR_MARKETDATA_TYPE", "4")

    feed = IbkrDataFeed(client_id=188)
    barrier = threading.Barrier(8)
    results: list[bool] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=2.0)
            results.append(feed._ensure_connected())
        except BaseException as exc:  # pragma: no cover - re-raised by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert not any(thread.is_alive() for thread in threads), "lazy connect deadlocked under concurrent access"
    assert errors == []
    assert results == [True] * 8
    assert connects == [("127.0.0.1", 4002, 188, 8)]
    assert market_data_types == [4]


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


def _real_bar(symbol: str, close: float = 100.0) -> dict:
    """Helper — emit a bar that passes _is_real_bar (non-flat OHLC + volume)."""
    return {
        "open": close - 0.1, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": 10.0, "ts": "2026-05-04T19:30:00Z",
        "symbol": symbol,
    }


def test_composite_routes_crypto_to_coinbase_and_futures_to_yfinance() -> None:
    """Default routing: crypto → coinbase, futures → yfinance (paper-
    friendly real-time-ish), other → yfinance fallback."""
    from eta_engine.scripts.data_feeds import CompositeDataFeed
    feed = CompositeDataFeed()

    fake_coinbase = MagicMock()
    fake_coinbase.get_bar.return_value = _real_bar("BTC", 80000.0)
    fake_yf = MagicMock()
    fake_yf.get_bar.return_value = _real_bar("MNQ1", 27500.0)
    feed._feeds["coinbase"] = fake_coinbase
    feed._feeds["yfinance"] = fake_yf

    feed.get_bar("BTC")
    feed.get_bar("MNQ1")
    feed.get_bar("AAPL")

    fake_coinbase.get_bar.assert_called_once_with("BTC")
    # YFinance handles futures AND fallback for stocks
    assert fake_yf.get_bar.call_count == 2


def test_composite_falls_back_when_primary_returns_empty() -> None:
    """If the primary feed returns _empty_bar, the composite must try
    the fallback chain instead of letting the supervisor see noise."""
    from eta_engine.scripts.data_feeds import CompositeDataFeed, _empty_bar
    feed = CompositeDataFeed()

    fake_coinbase = MagicMock()
    fake_coinbase.get_bar.return_value = _empty_bar("BTC")  # primary fails
    fake_yf = MagicMock()
    fake_yf.get_bar.return_value = _real_bar("BTC", 80000.0)
    feed._feeds["coinbase"] = fake_coinbase
    feed._feeds["yfinance"] = fake_yf

    bar = feed.get_bar("BTC")
    assert bar["close"] == 80000.0
    # Both feeds were tried; coinbase first, yfinance after
    fake_coinbase.get_bar.assert_called_once_with("BTC")
    fake_yf.get_bar.assert_called_once_with("BTC")


def test_composite_health_snapshot_tracks_per_feed_outcomes() -> None:
    """The composite records ok/empty counts per (feed, symbol-root) so
    the operator can see which feeds are actually serving data."""
    from eta_engine.scripts.data_feeds import CompositeDataFeed, _empty_bar
    feed = CompositeDataFeed()

    fake_coinbase = MagicMock()
    fake_coinbase.get_bar.side_effect = [
        _real_bar("BTC", 80000.0),
        _empty_bar("ETH"),
    ]
    fake_yf = MagicMock()
    fake_yf.get_bar.return_value = _real_bar("ETH", 2400.0)
    feed._feeds["coinbase"] = fake_coinbase
    feed._feeds["yfinance"] = fake_yf

    feed.get_bar("BTC")
    feed.get_bar("ETH")

    snap = feed.health_snapshot()
    assert snap["coinbase::BTC"] == {"ok": 1, "empty": 0}
    assert snap["coinbase::ETH"] == {"ok": 0, "empty": 1}
    assert snap["yfinance::ETH"] == {"ok": 1, "empty": 0}


def test_composite_respects_env_overrides() -> None:
    """ETA_FUTURES_FEED=ibkr forces TWS even though the new default is yfinance."""
    import os
    os.environ["ETA_FUTURES_FEED"] = "ibkr"
    try:
        from eta_engine.scripts.data_feeds import CompositeDataFeed
        feed = CompositeDataFeed()
        assert feed._futures_feed == "ibkr"
    finally:
        os.environ.pop("ETA_FUTURES_FEED", None)


def test_composite_propagates_feed_exceptions_to_fallback() -> None:
    """A feed raising mid-call must not break the composite's tick;
    fallback should still serve."""
    from eta_engine.scripts.data_feeds import CompositeDataFeed
    feed = CompositeDataFeed()

    fake_coinbase = MagicMock()
    fake_coinbase.get_bar.side_effect = RuntimeError("network down")
    fake_yf = MagicMock()
    fake_yf.get_bar.return_value = _real_bar("BTC", 80000.0)
    feed._feeds["coinbase"] = fake_coinbase
    feed._feeds["yfinance"] = fake_yf

    bar = feed.get_bar("BTC")
    assert bar["close"] == 80000.0
    # Stats record the exception as "empty" for the offending feed
    snap = feed.health_snapshot()
    assert snap["coinbase::BTC"]["empty"] >= 1


def test_is_real_bar_distinguishes_data_from_fallback() -> None:
    from eta_engine.scripts.data_feeds import _empty_bar, _is_real_bar
    assert not _is_real_bar(_empty_bar("BTC"))
    assert _is_real_bar({"open": 1.0, "high": 1.5, "low": 0.9, "close": 1.2, "volume": 10})
    assert _is_real_bar({"open": 1, "high": 1, "low": 1, "close": 1, "volume": 10})  # vol > 0
    # Flat OHLC + zero volume = empty
    assert not _is_real_bar({"open": 5, "high": 5, "low": 5, "close": 5, "volume": 0})


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
