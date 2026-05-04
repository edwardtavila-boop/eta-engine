"""Real data feeds for the JARVIS supervisor.

Replaces MockDataFeed (synthetic random-walk) with real-market sources:

* YFinanceDataFeed — Yahoo finance polling (universal: BTC-USD, ETH-USD,
  MNQ=F, NQ=F, etc.). Cached per-symbol with TTL so 24 bots on the
  same tick don't fan out 24 HTTP calls.
* CoinbaseDataFeed — Coinbase Exchange public candles API (crypto only,
  free, fast). BTC-USD / ETH-USD / SOL-USD / etc.
* IbkrDataFeed — TWS reqHistoricalData against the same gateway the
  venue uses. Best for futures (real exchange ticks), needs market-
  data subscription on the IBKR account for live data; without it
  returns delayed bars.
* CompositeDataFeed — symbol-type router: crypto → coinbase, futures
  → ibkr, fallback → yfinance.

Each feed exposes the same surface MockDataFeed does:

    get_bar(symbol: str) -> dict
        with keys: open, high, low, close, volume, ts, symbol

so the supervisor's tick loop is feed-agnostic.

Selection happens in JarvisStrategySupervisor.__init__ via the
``ETA_SUPERVISOR_FEED`` env (mock | yfinance | coinbase | ibkr |
composite). The factory ``make_data_feed`` is the single entry
point.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ─── Common surface ──────────────────────────────────────────────


class DataFeed(Protocol):
    """Minimal surface every supervisor feed must implement."""

    def get_bar(self, symbol: str) -> dict[str, Any]: ...


def _empty_bar(symbol: str, last_close: float = 100.0) -> dict[str, Any]:
    """Fallback bar shape — used when a real feed transiently fails so
    the supervisor doesn't crash mid-tick."""
    return {
        "open": last_close,
        "high": last_close,
        "low": last_close,
        "close": last_close,
        "volume": 0.0,
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
    }


# ─── Symbol mapping helpers ──────────────────────────────────────


_CRYPTO_ROOTS = {"BTC", "ETH", "SOL", "AVAX", "LINK", "DOGE", "MBT", "MET"}
_FUTURES_ROOTS = {
    "MNQ", "NQ", "ES", "MES", "NG", "CL", "GC", "ZN", "ZB",
    "6E", "M6E", "MGC", "MCL", "RTY", "M2K", "MBT", "MET",
}


def _root(symbol: str) -> str:
    """MNQ1 → MNQ, BTCUSD → BTC, ETHUSDT → ETH, etc.

    Strip USDT before USD so ETHUSDT doesn't become ETHT after the
    first replace eats the inner USD.
    """
    s = symbol.upper().lstrip("/").rstrip("0123456789")
    for suffix in ("USDT", "USD"):
        if s.endswith(suffix):
            s = s[: -len(suffix)] or s
            break
    return s


def _is_crypto(symbol: str) -> bool:
    return _root(symbol) in _CRYPTO_ROOTS


def _is_futures(symbol: str) -> bool:
    r = _root(symbol)
    # MBT/MET are crypto-futures hybrids; treat as crypto unless caller
    # explicitly maps them to futures via env (rare).
    if r in {"MBT", "MET"}:
        return False
    return r in _FUTURES_ROOTS


# ─── YFinance ────────────────────────────────────────────────────


class YFinanceDataFeed:
    """Universal yfinance poller with TTL cache.

    Symbol → yfinance ticker mapping:
      BTC / BTCUSD → BTC-USD
      ETH / ETHUSD → ETH-USD
      SOL / SOLUSD → SOL-USD
      MNQ / MNQ1   → MNQ=F
      NQ  / NQ1    → NQ=F
      ES  / ES1    → ES=F
      etc.

    Cache TTL defaults to 30s — at the supervisor's 60s tick, every
    other tick refetches; with 24 bots on the same tick, only one
    HTTP round-trip per unique symbol per ~30s window.
    """

    YF_MAP: dict[str, str] = {
        "BTC": "BTC-USD",
        "ETH": "ETH-USD",
        "SOL": "SOL-USD",
        "AVAX": "AVAX-USD",
        "LINK": "LINK-USD",
        "DOGE": "DOGE-USD",
        "MNQ": "MNQ=F",
        "NQ": "NQ=F",
        "ES": "ES=F",
        "MES": "MES=F",
        "NG": "NG=F",
        "CL": "CL=F",
        "GC": "GC=F",
        "ZN": "ZN=F",
        "RTY": "RTY=F",
        "6E": "6E=F",
        "MBT": "BTC-USD",  # micro BTC — track BTC-USD spot
        "MET": "ETH-USD",  # micro ETH — track ETH-USD spot
    }

    def __init__(self, *, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _yf_ticker(self, symbol: str) -> str | None:
        return self.YF_MAP.get(_root(symbol))

    def get_bar(self, symbol: str) -> dict[str, Any]:
        ticker = self._yf_ticker(symbol)
        if ticker is None:
            logger.warning("YFinanceDataFeed: no yfinance mapping for %s", symbol)
            return _empty_bar(symbol)

        now = time.time()
        with self._lock:
            cached = self._cache.get(ticker)
            if cached and (now - cached[0]) < self._ttl:
                bar = dict(cached[1])
                bar["symbol"] = symbol
                bar["ts"] = datetime.now(UTC).isoformat()
                return bar

        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="1d", interval="1m", auto_adjust=False)
            if hist is None or hist.empty:
                logger.warning("YFinanceDataFeed: empty history for %s", ticker)
                return _empty_bar(symbol)
            row = hist.iloc[-1]
            bar = {
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0) or 0),
                "ts": datetime.now(UTC).isoformat(),
                "symbol": symbol,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("YFinanceDataFeed fetch failed for %s: %s", ticker, exc)
            return _empty_bar(symbol)

        with self._lock:
            self._cache[ticker] = (now, dict(bar))
        return bar


# ─── Coinbase ────────────────────────────────────────────────────


class CoinbaseDataFeed:
    """Coinbase Exchange public candles API — crypto only, free.

    Endpoint: GET https://api.exchange.coinbase.com/products/{product}
    /candles?granularity=60 → array of [time, low, high, open, close,
    volume] in reverse-chronological order. We pull granularity=60 (1m
    bars) and return the most recent.

    Only crypto symbols are supported; futures fall back via
    CompositeDataFeed.
    """

    PRODUCT_MAP: dict[str, str] = {
        "BTC": "BTC-USD",
        "ETH": "ETH-USD",
        "SOL": "SOL-USD",
        "AVAX": "AVAX-USD",
        "LINK": "LINK-USD",
        "DOGE": "DOGE-USD",
        "MBT": "BTC-USD",
        "MET": "ETH-USD",
    }

    BASE_URL = "https://api.exchange.coinbase.com"

    def __init__(self, *, ttl_seconds: float = 15.0) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _product(self, symbol: str) -> str | None:
        return self.PRODUCT_MAP.get(_root(symbol))

    def get_bar(self, symbol: str) -> dict[str, Any]:
        product = self._product(symbol)
        if product is None:
            logger.warning("CoinbaseDataFeed: no product mapping for %s", symbol)
            return _empty_bar(symbol)

        now = time.time()
        with self._lock:
            cached = self._cache.get(product)
            if cached and (now - cached[0]) < self._ttl:
                bar = dict(cached[1])
                bar["symbol"] = symbol
                bar["ts"] = datetime.now(UTC).isoformat()
                return bar

        try:
            import requests
            url = f"{self.BASE_URL}/products/{product}/candles"
            resp = requests.get(url, params={"granularity": 60}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if not data or not isinstance(data, list):
                logger.warning("CoinbaseDataFeed: empty candles for %s", product)
                return _empty_bar(symbol)
            # First row is most recent: [time, low, high, open, close, volume]
            ts, low, high, op, close, vol = data[0]
            bar = {
                "open": float(op),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(vol),
                "ts": datetime.now(UTC).isoformat(),
                "symbol": symbol,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("CoinbaseDataFeed fetch failed for %s: %s", product, exc)
            return _empty_bar(symbol)

        with self._lock:
            self._cache[product] = (now, dict(bar))
        return bar


# ─── IBKR ────────────────────────────────────────────────────────


class IbkrDataFeed:
    """TWS reqHistoricalData feed for futures.

    Uses its OWN ib_insync.IB connection (clientId default 88, distinct
    from the venue's 99) so order traffic and bar-data traffic don't
    collide. Connects lazily on first get_bar call. Caches the latest
    1m bar per symbol.

    For paper accounts WITHOUT a market-data subscription, IBKR returns
    DELAYED data via reqMarketDataType(3); for live accounts with a
    subscription it returns real-time. The caller can override the
    market-data type via env ``ETA_IBKR_MARKETDATA_TYPE`` (1=live,
    2=frozen, 3=delayed, 4=delayed-frozen).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 88,
        ttl_seconds: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._ib: Any | None = None
        self._connect_lock = threading.Lock()

    def _ensure_connected(self) -> bool:
        if self._ib is not None and self._ib.isConnected():
            return True
        with self._connect_lock:
            if self._ib is not None and self._ib.isConnected():
                return True
            try:
                import os

                from ib_insync import IB
                self._ib = IB()
                self._ib.connect(self._host, self._port, clientId=self._client_id, timeout=8)
                mdt = int(os.getenv("ETA_IBKR_MARKETDATA_TYPE", "3"))
                self._ib.reqMarketDataType(mdt)
                logger.info(
                    "IbkrDataFeed connected to TWS (clientId=%d, marketDataType=%d)",
                    self._client_id, mdt,
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("IbkrDataFeed connect failed: %s", exc)
                return False

    def _make_contract(self, symbol: str) -> Any | None:  # noqa: ANN401 — ib_insync Contract
        """Resolve a symbol to an ib_insync Contract.

        Reuses the venue's FUTURES_MAP / CONTRACT_MONTH so MNQ1 / NQ1
        / etc. work the same way the order path does.
        """
        try:
            from eta_engine.venues.ibkr_live import (
                CONTRACT_MONTH,
                FUTURES_MAP,
            )
            from ib_insync import Future
        except ImportError:
            return None

        sym = _root(symbol).upper()
        # Pull the underlying root for futures-month suffixed names
        if sym in FUTURES_MAP:
            root, exchange, mult = FUTURES_MAP[sym]
            contract = Future(symbol=root, exchange=exchange, currency="USD")
            contract.lastTradeDateOrContractMonth = CONTRACT_MONTH
            contract.multiplier = mult
            contract.includeExpired = False
            return contract
        return None

    def get_bar(self, symbol: str) -> dict[str, Any]:
        if not self._ensure_connected():
            return _empty_bar(symbol)

        contract = self._make_contract(symbol)
        if contract is None:
            logger.warning("IbkrDataFeed: no contract for %s", symbol)
            return _empty_bar(symbol)

        now = time.time()
        cache_key = _root(symbol)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and (now - cached[0]) < self._ttl:
                bar = dict(cached[1])
                bar["symbol"] = symbol
                bar["ts"] = datetime.now(UTC).isoformat()
                return bar

        try:
            # 30-min window of 1m bars: short enough to keep the call
            # cheap but long enough that IB always has data for the
            # current contract. 60s is rejected for futures.
            bars = self._ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1800 S",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
            )
            if not bars:
                logger.warning("IbkrDataFeed: empty bars for %s", symbol)
                return _empty_bar(symbol)
            b = bars[-1]
            bar = {
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume) if b.volume else 0.0,
                "ts": datetime.now(UTC).isoformat(),
                "symbol": symbol,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("IbkrDataFeed fetch failed for %s: %s", symbol, exc)
            return _empty_bar(symbol)

        with self._cache_lock:
            self._cache[cache_key] = (now, dict(bar))
        return bar


# ─── Composite router with fallback chains ──────────────────────


def _is_real_bar(bar: dict[str, Any]) -> bool:
    """A bar is 'real' iff at least one of OHLC differs from the
    fallback flat-line. _empty_bar emits open=high=low=close=last_close
    + volume=0; any feed that actually sourced data has either
    non-zero volume or non-flat OHLC."""
    if bar.get("volume", 0) > 0:
        return True
    o, h, low, c = bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")
    return not (o == h == low == c)


class CompositeDataFeed:
    """Per-symbol-type routing with fallback chains and health stats.

    Default routing (overridable via env):

      Crypto symbols  → CoinbaseDataFeed    → YFinanceDataFeed (fallback)
      Futures symbols → YFinanceDataFeed    → IbkrDataFeed (fallback)
      Anything else   → YFinanceDataFeed

    Why yfinance is now the default for futures: IBKR paper accounts
    without a market-data subscription fall back to delayed quotes
    (~15 min). Yahoo's futures feed (MNQ=F, NQ=F, etc) is real-time-ish
    (~1-5s lag) and free, so paper fine-tuning gets honest data without
    requiring an IBKR subscription. Operators with a live IBKR market-
    data subscription should set ``ETA_FUTURES_FEED=ibkr`` to use the
    real-time TWS path instead.

    Env overrides:
      ETA_CRYPTO_FEED   = coinbase | yfinance | ibkr  (default: coinbase)
      ETA_FUTURES_FEED  = yfinance | ibkr | coinbase  (default: yfinance)
      ETA_FALLBACK_FEED = yfinance | ibkr | coinbase | none  (default: yfinance)

    Each get_bar call records success/empty per (feed, symbol) so the
    supervisor heartbeat can surface drift. Read via .health_snapshot().
    """

    def __init__(self) -> None:
        self._feeds: dict[str, Any] = {}
        self._feed_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats: dict[str, dict[str, int]] = {}

        import os
        self._crypto_feed = os.getenv("ETA_CRYPTO_FEED", "coinbase").strip().lower()
        self._futures_feed = os.getenv("ETA_FUTURES_FEED", "yfinance").strip().lower()
        self._fallback_feed = os.getenv("ETA_FALLBACK_FEED", "yfinance").strip().lower()

    def _build(self, name: str) -> Any:  # noqa: ANN401 — multiple feed classes share the protocol
        if name == "coinbase":
            return CoinbaseDataFeed()
        if name == "yfinance":
            return YFinanceDataFeed()
        if name == "ibkr":
            return IbkrDataFeed()
        return None

    def _get_feed(self, name: str) -> Any:  # noqa: ANN401
        if name in {"none", "", "off"}:
            return None
        with self._feed_lock:
            if name not in self._feeds:
                self._feeds[name] = self._build(name)
            return self._feeds[name]

    def _record(self, feed_name: str, symbol: str, ok: bool) -> None:
        with self._stats_lock:
            key = f"{feed_name}::{_root(symbol)}"
            slot = self._stats.setdefault(key, {"ok": 0, "empty": 0})
            slot["ok" if ok else "empty"] += 1

    def health_snapshot(self) -> dict[str, dict[str, int]]:
        with self._stats_lock:
            return {k: dict(v) for k, v in self._stats.items()}

    def _try_feed(self, feed_name: str, symbol: str) -> dict[str, Any] | None:
        feed = self._get_feed(feed_name)
        if feed is None:
            return None
        try:
            bar = feed.get_bar(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CompositeDataFeed: %s raised on %s: %s",
                feed_name, symbol, exc,
            )
            self._record(feed_name, symbol, ok=False)
            return None
        if _is_real_bar(bar):
            self._record(feed_name, symbol, ok=True)
            return bar
        self._record(feed_name, symbol, ok=False)
        return None

    def get_bar(self, symbol: str) -> dict[str, Any]:
        # Pick the chain based on symbol type; primary first, fallback
        # second if primary returned an _empty_bar.
        if _is_crypto(symbol):
            primary, fallback = self._crypto_feed, self._fallback_feed
        elif _is_futures(symbol):
            primary, fallback = self._futures_feed, "ibkr" if self._futures_feed != "ibkr" else self._fallback_feed
        else:
            primary, fallback = self._fallback_feed, "none"

        bar = self._try_feed(primary, symbol)
        if bar is not None:
            return bar
        if fallback and fallback != primary:
            bar = self._try_feed(fallback, symbol)
            if bar is not None:
                logger.info(
                    "CompositeDataFeed: %s served %s after %s missed",
                    fallback, symbol, primary,
                )
                return bar
        # Both primary and fallback returned empty bars — final fallback
        # is YFinance if neither was already YFinance, else just empty.
        if "yfinance" not in {primary, fallback}:
            bar = self._try_feed("yfinance", symbol)
            if bar is not None:
                return bar
        return _empty_bar(symbol)


# ─── Factory ─────────────────────────────────────────────────────


def make_data_feed(name: str) -> Any:  # noqa: ANN401 — multiple feed classes share the DataFeed protocol
    """Build the data feed selected by ``ETA_SUPERVISOR_FEED``.

    Falls back to the synthetic MockDataFeed when ``name`` is anything
    other than the known real-data values, so a fat-fingered env var
    can't silently disable trading.
    """
    n = (name or "mock").strip().lower()
    if n == "yfinance":
        return YFinanceDataFeed()
    if n == "coinbase":
        return CoinbaseDataFeed()
    if n == "ibkr":
        return IbkrDataFeed()
    if n == "composite":
        return CompositeDataFeed()
    # Mock is built lazily by the caller (it lives in
    # jarvis_strategy_supervisor to keep the feed module dependency-free).
    from eta_engine.scripts.jarvis_strategy_supervisor import MockDataFeed
    return MockDataFeed()
