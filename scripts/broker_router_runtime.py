from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from logging import Logger


class _SignalLoop(Protocol):
    def add_signal_handler(self, sig: object, callback: Callable[[], None]) -> None: ...


class BrokerRouterRuntimeControl:
    """Own stop signaling plus the outer async run/run_once wrapper."""

    def __init__(
        self,
        *,
        pending_dir: Path,
        state_root: Path,
        dry_run: bool,
        interval_s: float,
        is_stopped: Callable[[], bool],
        set_stopped: Callable[[bool], None],
        tick: Callable[[], Awaitable[None]],
        logger: Logger,
        get_running_loop: Callable[[], _SignalLoop] = asyncio.get_running_loop,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        signals: Sequence[object] = (signal.SIGINT, signal.SIGTERM),
    ) -> None:
        self._pending_dir = Path(pending_dir)
        self._state_root = Path(state_root)
        self._dry_run = bool(dry_run)
        self._interval_s = float(interval_s)
        self._is_stopped = is_stopped
        self._set_stopped = set_stopped
        self._tick = tick
        self._logger = logger
        self._get_running_loop = get_running_loop
        self._sleep = sleep
        self._signals = tuple(signals)

    def request_stop(self) -> None:
        """Signal the run loop to drain and exit on the next boundary."""
        self._set_stopped(True)

    async def run(self) -> None:
        """Main poll loop. Stops on SIGINT/SIGTERM or request_stop()."""
        loop = self._get_running_loop()
        for sig in self._signals:
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(sig, self.request_stop)

        self._logger.info(
            "broker_router starting pending=%s state=%s dry_run=%s interval_s=%.1f",
            self._pending_dir,
            self._state_root,
            self._dry_run,
            self._interval_s,
        )
        while not self._is_stopped():
            await self._tick()
            if self._is_stopped():
                break
            try:
                await self._sleep(self._interval_s)
            except asyncio.CancelledError:
                break
        self._logger.info("broker_router stopped")

    async def run_once(self) -> None:
        """Single-pass scan + heartbeat. Used by --once and tests."""
        await self._tick()
