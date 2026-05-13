from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.core.data_pipeline import BarData

logger = logging.getLogger(__name__)

_IB_GATEWAY_HOST = "127.0.0.1"
_IB_GATEWAY_PORT = 7497
_IB_CLIENT_ID = 77


class IbGatewayFeed:
    def __init__(
        self,
        *,
        host: str = _IB_GATEWAY_HOST,
        port: int = _IB_GATEWAY_PORT,
        client_id: int = _IB_CLIENT_ID,
        symbol: str = "MNQ",
        timeframe: str = "5m",
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.symbol = symbol
        self.timeframe = timeframe
        self._ib: object = None
        self._connected = False
        self._running = False
        self._bar_callbacks: list[Callable] = []
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        try:
            from ib_insync import IB
        except ImportError:
            logger.error("ib_insync not installed. Run: pip install ib_insync")
            return False
        try:
            ib = IB()
            asyncio.get_event_loop()
            ib.connect(self.host, self.port, clientId=self.client_id, timeout=15)
            self._ib = ib
            self._connected = True
            logger.info(
                "IB Gateway feed connected: %s:%s client=%s",
                self.host,
                self.port,
                self.client_id,
            )
            return True
        except Exception as exc:
            logger.error("IB Gateway connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception as exc:
                logger.warning("IB Gateway disconnect: %s", exc)
        self._connected = False

    def on_bar(self, callback: Callable) -> None:
        self._bar_callbacks.append(callback)

    async def start_stream(self) -> None:
        if not self._connected or self._ib is None:
            raise RuntimeError("Feed not connected. Call connect() first.")
        self._running = True
        self._task = asyncio.create_task(self._run_stream())

    async def start_historical(self, days: int = 5) -> list[BarData]:
        """Pull historical bars first, then start live streaming."""
        from eta_engine.core.data_pipeline import BarData

        if not self._connected or self._ib is None:
            raise RuntimeError("Feed not connected")
        try:
            from ib_insync import Future
        except ImportError:
            return []
        bars: list[BarData] = []
        contract = Future(self.symbol, includeExpired=False)
        now = datetime.now(UTC)
        duration = f"{days} D"
        bar_size = _timeframe_to_ib(self.timeframe)
        try:
            hist = self._ib.reqHistoricalData(
                contract,
                endDateTime=now.strftime("%Y%m%d %H:%M:%S UTC"),
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
                keepUpToDate=False,
            )
            _timeframe_seconds(self.timeframe)
            for h in hist:
                bars.append(
                    BarData(
                        timestamp=h.date if hasattr(h.date, "tzinfo") else h.date.replace(tzinfo=UTC),
                        symbol=self.symbol,
                        open=float(h.open),
                        high=float(h.high),
                        low=float(h.low),
                        close=float(h.close),
                        volume=float(h.volume),
                    )
                )
            logger.info("IB Gateway: loaded %d historical %s bars", len(bars), self.timeframe)
        except Exception as exc:
            logger.warning("IB Gateway historical data failed: %s", exc)
        return bars

    async def _run_stream(self) -> None:
        try:
            from ib_insync import Future
        except ImportError:
            logger.error("ib_insync not installed. Run: pip install ib_insync")
            return
        self._ib.disconnectedEvent += self._on_disconnected
        contract = Future(self.symbol, includeExpired=False)
        self._ib.reqMktData(contract, "", False, False)
        bars: dict[str, dict] = {}

        while self._running and self._connected:
            try:
                await asyncio.sleep(0.5)
                ticker = self._ib.ticker(contract)
                if ticker is None:
                    continue
                ts = datetime.now(UTC)
                price = float(ticker.last or ticker.close or 0)
                if price <= 0:
                    continue
                bucket_key = _bucket_key(ts, self.timeframe)
                if bucket_key not in bars:
                    bars[bucket_key] = {
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": 0,
                        "ts": ts,
                    }
                bar = bars[bucket_key]
                bar["high"] = max(bar["high"], price)
                bar["low"] = min(bar["low"], price)
                bar["close"] = price
                if ticker.last:
                    last_size = getattr(ticker, "lastSize", ticker.size) or 0
                    bar["volume"] += int(last_size) if hasattr(last_size, "__int__") else 1

                # Emit completed bars (bucket changed)
                completed = [k for k in bars if k != bucket_key]
                for k in completed:
                    completed_bar = bars.pop(k)
                    self._emit_bar(completed_bar)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("IB Gateway stream error: %s", exc)
                await asyncio.sleep(5)

    def _emit_bar(self, bar_data: dict) -> None:
        from eta_engine.core.data_pipeline import BarData

        bar = BarData(
            timestamp=bar_data["ts"],
            symbol=self.symbol,
            open=round(bar_data["open"], 2),
            high=round(bar_data["high"], 2),
            low=round(bar_data["low"], 2),
            close=round(bar_data["close"], 2),
            volume=float(bar_data["volume"]),
        )
        for cb in self._bar_callbacks:
            try:
                cb(bar)
            except Exception as exc:
                logger.warning("IB Gateway bar callback error: %s", exc)

    def _on_disconnected(self) -> None:
        logger.warning("IB Gateway disconnected")
        self._connected = False


def _timeframe_to_ib(timeframe: str) -> str:
    mapping = {"1m": "1 min", "5m": "5 mins", "15m": "15 mins", "1h": "1 hour", "1d": "1 day"}
    return mapping.get(timeframe, "5 mins")


def _timeframe_seconds(timeframe: str) -> int:
    mapping = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
    return mapping.get(timeframe, 300)


def _bucket_key(ts: datetime, timeframe: str) -> str:
    secs = _timeframe_seconds(timeframe)
    bucket_ts = int(ts.timestamp()) // secs * secs
    return str(bucket_ts)
