from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

IBKR_BASE = "https://127.0.0.1:5000/v1/api"
FIELDS = "31,84,85,86,88,7059"
DEFAULT_POLL_S = 1.0
DEFAULT_TIMEFRAME = "5m"

_TIMEFRAME_SECS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

SYMBOL_CONFIG: dict[str, dict] = {
    "MNQ": {"conid": 770561201, "type": "futures", "exchange": "CME"},
    "NQ":  {"conid": 750150196, "type": "futures", "exchange": "CME"},
    "ES":  {"conid": 649180678, "type": "futures", "exchange": "CME"},
    "BTC": {"conid": 764777976, "type": "crypto", "exchange": "PAXOS"},
    "ETH": {"conid": 764777977, "type": "crypto", "exchange": "PAXOS"},
}


def _bucket(ts: datetime, timeframe: str) -> datetime:
    secs = _TIMEFRAME_SECS.get(timeframe, 300)
    return datetime.fromtimestamp((ts.timestamp() // secs) * secs, tz=UTC)


@dataclass
class SymbolBar:
    symbol: str
    timestamp: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    bid: float = 0.0
    ask: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "open": round(self.open, 2),
            "high": round(self.high, 2),
            "low": round(self.low, 2),
            "close": round(self.close, 2),
            "volume": self.volume,
            "bid": round(self.bid, 2),
            "ask": round(self.ask, 2),
        }


class MultiSymbolFeed:
    def __init__(
        self,
        *,
        symbols: list[str] | None = None,
        timeframe: str = DEFAULT_TIMEFRAME,
        poll_interval_s: float = DEFAULT_POLL_S,
    ) -> None:
        self.timeframe = timeframe
        self.poll_interval_s = max(0.25, poll_interval_s)
        self._configs = {sym: SYMBOL_CONFIG[sym] for sym in (symbols or list(SYMBOL_CONFIG)) if sym in SYMBOL_CONFIG}
        self._session: object = None
        self._connected = False
        self._running = False
        self._task: asyncio.Task | None = None
        self._bars: dict[str, SymbolBar] = {}
        self._callbacks: list[Callable] = []

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def bar_count(self) -> int:
        return len(self._bars)

    def get_bar(self, symbol: str) -> SymbolBar | None:
        return self._bars.get(symbol)

    def all_bars(self) -> dict[str, dict]:
        return {s: b.to_dict() for s, b in self._bars.items()}

    def on_bar(self, callback: Callable) -> None:
        self._callbacks.append(callback)

    async def connect(self) -> bool:
        import ssl

        import aiohttp
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            self._session = aiohttp.ClientSession(
                base_url=IBKR_BASE + "/",
                connector=aiohttp.TCPConnector(ssl=ctx),
                timeout=aiohttp.ClientTimeout(total=10),
            )
            async with self._session.get("iserver/auth/status") as r:
                body = await r.json()
                if body.get("authenticated"):
                    await self._session.get("iserver/accounts")
                    self._connected = True
                    logger.info("MultiSymbolFeed: authenticated, %d symbols", len(self._configs))
                    return True
                logger.warning("MultiSymbolFeed: not authenticated")
                return False
        except Exception as exc:
            logger.error("MultiSymbolFeed: connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False

    async def start_stream(self) -> None:
        if not self._connected:
            raise RuntimeError("Not connected")
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        conids = {sym: cfg["conid"] for sym, cfg in self._configs.items()}
        buckets: dict[str, datetime] = {}
        bar_states: dict[str, dict] = {}

        for sym in conids:
            bar_states[sym] = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0, "bid": 0.0, "ask": 0.0}

        while self._running:
            try:
                data = await self._poll(conids)
                now = datetime.now(UTC)
                for sym, tick in data.items():
                    price = tick.get("last", 0) or (tick.get("bid", 0) + tick.get("ask", 0)) / 2
                    if price <= 0:
                        continue
                    bucket = _bucket(now, self.timeframe)
                    bs = bar_states[sym]
                    if sym not in buckets or bucket != buckets[sym]:
                        if sym in buckets:
                            self._emit_bar(sym, buckets[sym], bs)
                        buckets[sym] = bucket
                        bs["open"] = price
                        bs["high"] = price
                        bs["low"] = price
                        bs["close"] = price
                        bs["volume"] = tick.get("volume", 0)
                    else:
                        bs["high"] = max(bs["high"], price)
                        bs["low"] = min(bs["low"], price) if bs["low"] > 0 else price
                        bs["close"] = price
                        bs["volume"] += tick.get("volume", 0)
                    bs["bid"] = tick.get("bid", 0)
                    bs["ask"] = tick.get("ask", 0)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("MultiSymbolFeed poll error: %s", exc)
                await asyncio.sleep(self.poll_interval_s * 5)
                continue
            await asyncio.sleep(self.poll_interval_s)

    async def _poll(self, conids: dict[str, int]) -> dict[str, dict]:
        if not self._session:
            return {}
        conid_str = ",".join(str(c) for c in conids.values())
        async with self._session.get(
            "iserver/marketdata/snapshot",
            params={"conids": conid_str, "fields": FIELDS},
        ) as r:
            if r.status != 200:
                return {}
            body = await r.json()

        symbol_map = {v: k for k, v in conids.items()}
        result: dict[str, dict] = {}
        for item in body if isinstance(body, list) else [body]:
            cid = item.get("conid", "")
            sym = symbol_map.get(int(cid) if isinstance(cid, (int, str)) and str(cid).isdigit() else cid)
            if sym:
                result[sym] = {
                    "last": float(item.get("31", 0) or 0),
                    "bid": float(item.get("84", 0) or 0),
                    "ask": float(item.get("86", 0) or 0),
                    "volume": int(float(item.get("7059", 0) or 0)),
                }
        return result

    def _emit_bar(self, symbol: str, ts: datetime, state: dict) -> None:
        bar = SymbolBar(
            symbol=symbol,
            timestamp=ts,
            open=round(state["open"], 2),
            high=round(state["high"], 2),
            low=round(state["low"], 2),
            close=round(state["close"], 2),
            volume=state["volume"],
            bid=round(state["bid"], 2),
            ask=round(state["ask"], 2),
        )
        self._bars[symbol] = bar
        for cb in self._callbacks:
            try:
                cb(bar)
            except Exception as exc:
                logger.warning("callback error: %s", exc)

    def summary(self) -> dict:
        return {
            "connected": self._connected,
            "running": self._running,
            "symbols": list(self._configs),
            "bar_count": self.bar_count,
            "latest": self.all_bars(),
        }
