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

_IBKR_BASE_URL = "https://127.0.0.1:5000/v1/api"
_MARKET_DATA_FIELDS = "31,84,85,86,88,7059"
_DEFAULT_POLL_INTERVAL_S = 1.0
_TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900}

_MNQ_CONID_DEFAULT = 770561201


@dataclass
class _QuoteTick:
    symbol: str
    conid: int
    last: float
    bid: float
    ask: float
    volume: int
    ts: datetime


def _bucket_start(ts: datetime, timeframe: str) -> datetime:
    secs = _TIMEFRAME_SECONDS.get(timeframe, 60)
    bucket_ts = ts.timestamp()
    bucket_ts = (bucket_ts // secs) * secs
    return datetime.fromtimestamp(bucket_ts, tz=UTC)


def _quote_from_snapshot(raw: dict, conid: int) -> _QuoteTick | None:
    try:
        last_str = raw.get("31", "")
        bid_str = raw.get("84", "")
        ask_str = raw.get("86", "")
        vol_str = raw.get("7059", "")
        if not last_str:
            return None
        return _QuoteTick(
            symbol=str(raw.get("conid", conid)),
            conid=conid,
            last=float(last_str) if last_str else 0.0,
            bid=float(bid_str) if bid_str else 0.0,
            ask=float(ask_str) if ask_str else 0.0,
            volume=int(float(vol_str)) if vol_str else 0,
            ts=datetime.now(UTC),
        )
    except (ValueError, TypeError):
        return None


class IbkrFeed:
    def __init__(
        self,
        *,
        base_url: str = _IBKR_BASE_URL,
        conid: int = _MNQ_CONID_DEFAULT,
        symbol: str = "MNQ",
        timeframe: str = "5m",
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.conid = conid
        self.symbol = symbol
        self.timeframe = timeframe
        self.poll_interval_s = max(0.25, poll_interval_s)
        self._session: object = None
        self._connected = False
        self._running = False
        self._bar_callbacks: list[Callable] = []
        self._task: asyncio.Task | None = None
        self._current_bucket: datetime | None = None
        self._open_price = 0.0
        self._high = 0.0
        self._low = 0.0
        self._close = 0.0
        self._volume = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        try:
            import ssl

            import aiohttp

            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            self._session = aiohttp.ClientSession(
                base_url=self.base_url,
                connector=aiohttp.TCPConnector(ssl=ssl_ctx),
                timeout=aiohttp.ClientTimeout(total=10),
            )
            async with self._session.get("iserver/auth/status") as resp:
                body = await resp.json()
                if body.get("authenticated"):
                    # Initialize account context (required for market data snapshots)
                    with contextlib.suppress(Exception):
                        await self._session.get("iserver/accounts")
                    self._connected = True
                    logger.info(
                        "IBKR feed connected: authenticated=%s connected=%s",
                        body.get("authenticated"),
                        body.get("connected"),
                    )
                    return True
                logger.warning("IBKR gateway not authenticated: %s", body)
                return False
        except Exception as exc:
            logger.error("IBKR feed connect failed: %s", exc)
            self._connected = False
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._connected = False

    def on_bar(self, callback: Callable) -> None:
        self._bar_callbacks.append(callback)

    def on_tick(self, callback: Callable) -> None:
        pass

    def on_l2(self, callback: Callable) -> None:
        pass

    async def subscribe(self, symbols: list[str]) -> None:
        pass

    async def start_stream(self) -> None:
        if not self._connected:
            raise RuntimeError("Feed not connected. Call connect() first.")
        self._running = True
        self._task = asyncio.create_task(self._run_stream())

    async def _run_stream(self) -> None:
        self._reset_bucket()
        while self._running and self._connected:
            try:
                tick = await self._poll_snapshot()
                if tick is None:
                    await asyncio.sleep(self.poll_interval_s)
                    continue
                self._aggregate_tick(tick)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("IBKR feed poll error: %s", exc)
                await asyncio.sleep(self.poll_interval_s * 5)

    async def _poll_snapshot(self) -> _QuoteTick | None:
        if self._session is None:
            return None
        try:
            async with self._session.get(
                "iserver/marketdata/snapshot",
                params={"conids": str(self.conid), "fields": _MARKET_DATA_FIELDS},
            ) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
                items = body if isinstance(body, list) else [body]
                for item in items:
                    tick = _quote_from_snapshot(item, self.conid)
                    if tick is not None:
                        return tick
                return None
        except Exception:
            return None

    def _reset_bucket(self) -> None:
        self._current_bucket = None
        self._open_price = 0.0
        self._high = 0.0
        self._low = 0.0
        self._close = 0.0
        self._volume = 0

    def _aggregate_tick(self, tick: _QuoteTick) -> None:
        price = tick.last if tick.last > 0 else (tick.bid + tick.ask) / 2
        if price <= 0:
            return
        bucket = _bucket_start(tick.ts, self.timeframe)
        if self._current_bucket is None:
            self._current_bucket = bucket
            self._open_price = price
            self._high = price
            self._low = price
            self._close = price
            self._volume = tick.volume
            return
        if bucket != self._current_bucket:
            self._emit_bar()
            self._current_bucket = bucket
            self._open_price = price
            self._high = price
            self._low = price
            self._close = price
            self._volume = tick.volume
            return
        self._high = max(self._high, price)
        self._low = min(self._low, price) if self._low > 0 else price
        self._close = price
        self._volume += tick.volume

    def _emit_bar(self) -> None:
        from eta_engine.core.data_pipeline import BarData

        if self._current_bucket is None:
            return
        bar = BarData(
            timestamp=self._current_bucket,
            symbol=self.symbol,
            open=round(self._open_price, 2),
            high=round(self._high, 2),
            low=round(self._low, 2),
            close=round(self._close, 2),
            volume=float(self._volume),
        )
        for cb in self._bar_callbacks:
            try:
                cb(bar)
            except Exception as exc:
                logger.warning("IBKR feed bar callback error: %s", exc)

    def latest_bar(self) -> dict | None:
        if self._current_bucket is None:
            return None
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self._current_bucket.isoformat(),
            "open": round(self._open_price, 2),
            "high": round(self._high, 2),
            "low": round(self._low, 2),
            "close": round(self._close, 2),
            "volume": self._volume,
        }

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "running": self._running,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "conid": self.conid,
            "observers": len(self._bar_callbacks),
        }
