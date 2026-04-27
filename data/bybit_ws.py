"""
EVOLUTIONARY TRADING ALGO  //  data.bybit_ws
================================
Bybit v5 public websocket capture with rolling JSONL+gzip output.
Exponential reconnect backoff (1s -> 60s, cap 10 retries).

Design
------
* Real websocket via the `websockets` package (imported lazily so this module
  stays importable in environments that only want the replay / writer logic).
* When `websockets` is missing OR `stub=True` is passed, the client falls
  back to the original stubbed _connect / _subscribe / _recv_loop, keeping
  the existing unit tests green.
* Dumps each inbound frame as JSON-line into
  `data_root/<symbol>/<YYYY-MM-DD>.jsonl.gz`, rotating on UTC date change.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_URL = "wss://stream.bybit.com/v5/public/linear"
_PING_INTERVAL_S = 20.0
_WS_OPEN_TIMEOUT_S = 10.0


class BybitWSCapture:
    """Async capture to per-day gzipped JSONL under ~/apex_data/bybit/.

    Real-network path uses the `websockets` package. If the package is not
    available (or `stub=True`), the capture stays in its legacy no-op mode
    so unit tests don't need network or a monkeypatch.
    """

    _DEFAULT_TOPICS = ("kline.1", "publicTrade", "orderbook.50", "funding")

    def __init__(
        self,
        symbols: list[str],
        url: str = DEFAULT_URL,
        data_root: Path | None = None,
        topics: tuple[str, ...] = _DEFAULT_TOPICS,
        max_retries: int = 10,
        stub: bool = False,
    ) -> None:
        self.symbols = symbols
        self.url = url
        self.topics = topics
        self.max_retries = max_retries
        self.data_root = data_root or Path.home() / "apex_data" / "bybit"
        self.stub = stub
        self._running: bool = False
        self._ws: Any = None
        self._file_handles: dict[str, object] = {}
        self._current_date: str | None = None
        self._retry_count: int = 0
        self._alert_fired: bool = False
        self._ping_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def run_once(self) -> bool:
        """Single connect+subscribe+recv cycle.

        Returns True on clean exit (peer closed, iterator exhausted),
        False on transport error. Does NOT reconnect. Useful for tests and
        for callers that want explicit loop control.
        """
        try:
            await self._connect()
            await self._subscribe()
            await self._recv_loop()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("bybit ws transport error: %s", e)
            return False
        finally:
            await self._close_ws()

    async def start(self, stop_on_clean_close: bool = False) -> None:
        """Start capture loop. Reconnects with exp backoff up to max_retries.

        If `stop_on_clean_close` is True, exits when the peer cleanly closes
        the connection (no reconnect) - useful in tests / one-shot captures.
        """
        self._running = True
        delay = 1.0
        while self._running and self._retry_count < self.max_retries:
            ok = await self.run_once()
            if ok:
                # clean disconnect: reset backoff
                delay = 1.0
                self._retry_count = 0
                if stop_on_clean_close:
                    break
            else:
                self._retry_count += 1
                log.warning(
                    "bybit ws retry %d/%d after %.1fs",
                    self._retry_count,
                    self.max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 60.0)
        if self._retry_count >= self.max_retries and not self._alert_fired:
            self._alert_fired = True
            log.error("bybit ws max retries (%d) reached - alert!", self.max_retries)
        await self._flush_all()

    async def stop(self) -> None:
        """Signal the capture loop to stop and flush files."""
        self._running = False
        await self._close_ws()
        await self._flush_all()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    async def _connect(self) -> None:
        if self.stub:
            log.debug("[stub] connect to %s", self.url)
            self._ws = object()
            return
        try:
            import websockets  # noqa: PLC0415 - lazy import keeps module importable without websockets
        except ImportError:
            log.warning("websockets not installed - falling back to stub mode")
            self.stub = True
            self._ws = object()
            return
        self._ws = await asyncio.wait_for(
            websockets.connect(
                self.url,
                ping_interval=_PING_INTERVAL_S,
                ping_timeout=_PING_INTERVAL_S,
                open_timeout=_WS_OPEN_TIMEOUT_S,
            ),
            timeout=_WS_OPEN_TIMEOUT_S,
        )
        log.info("bybit ws connected url=%s topics=%d symbols=%d", self.url, len(self.topics), len(self.symbols))

    async def _subscribe(self) -> None:
        args = [f"{topic}.{sym}" for topic in self.topics for sym in self.symbols]
        payload = {"op": "subscribe", "args": args}
        if self.stub or self._ws is None or isinstance(self._ws, object.__class__) and not hasattr(self._ws, "send"):
            log.debug("[stub] subscribe %d topics", len(args))
            return
        await self._ws.send(json.dumps(payload))
        log.debug("bybit ws subscribed: %d topic-symbol pairs", len(args))

    async def _recv_loop(self) -> None:
        """Consume frames until the peer closes or we stop."""
        if self.stub or self._ws is None or not hasattr(self._ws, "__aiter__"):
            return
        async for raw in self._ws:
            if not self._running:
                break
            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            # Skip ack/pong/system frames
            if msg.get("op") in {"subscribe", "ping", "pong", "auth"}:
                continue
            if "topic" not in msg:
                continue
            await self._handle_message(msg)

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None or not hasattr(ws, "close"):
            return
        with contextlib.suppress(Exception):
            await ws.close()

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------
    async def _handle_message(self, msg: dict) -> None:
        topic = msg.get("topic", "")
        sym = self._symbol_from_topic(topic)
        if not sym:
            return
        await self._write_line(sym, msg)

    @staticmethod
    def _symbol_from_topic(topic: str) -> str | None:
        # "kline.1.ETHUSDT" -> "ETHUSDT"
        parts = topic.split(".")
        return parts[-1] if parts else None

    # ------------------------------------------------------------------
    # Rolling-file writer
    # ------------------------------------------------------------------
    async def _write_line(self, symbol: str, payload: dict) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if today != self._current_date:
            await self._rotate(today)
        key = symbol
        fh = self._file_handles.get(key)
        if fh is None:
            fh = self._open_for(symbol, today)
            self._file_handles[key] = fh
        payload["_recv_ts"] = time.time()
        fh.write((json.dumps(payload) + "\n").encode("utf-8"))  # type: ignore[union-attr]

    def _open_for(self, symbol: str, date: str) -> object:
        sym_dir = self.data_root / symbol
        sym_dir.mkdir(parents=True, exist_ok=True)
        path = sym_dir / f"{date}.jsonl.gz"
        return gzip.open(path, "ab")

    async def _rotate(self, new_date: str) -> None:
        await self._flush_all()
        self._current_date = new_date

    async def _flush_all(self) -> None:
        for fh in list(self._file_handles.values()):
            with contextlib.suppress(Exception):
                fh.close()  # type: ignore[union-attr]
        self._file_handles.clear()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def alert_fired(self) -> bool:
        return self._alert_fired
