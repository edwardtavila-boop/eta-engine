"""
EVOLUTIONARY TRADING ALGO  //  obs.watchdog
===========================================
Systemd ``sd_notify`` watchdog wiring for long-running daemons.

Without this module, ``Restart=always`` only catches process death.
A daemon that hangs (deadlocked thread, blocked on a TCP read, GC
pause that exceeds the broker's 30s WS heartbeat) keeps its PID alive
and silently fails. The systemd watchdog cures that: the unit declares
``WatchdogSec=30``, and the daemon must call ``sd_notify("WATCHDOG=1")``
at least every 30s. Miss two consecutive pings -> systemd kills + restarts.

This module implements a small, dependency-free sd_notify client (no
``systemd-python`` C extension required -- it's just an AF_UNIX
``SOCK_DGRAM`` write to ``$NOTIFY_SOCKET``) plus a ``WatchdogPinger``
helper that runs the keepalive in a background asyncio task.

Public API
----------

* :func:`sd_notify(state)`  -- raw notify; returns True iff sent.
* :func:`notify_ready()`    -- send ``READY=1`` (call from ``Type=notify`` daemon).
* :func:`notify_stopping()` -- send ``STOPPING=1`` (call before clean shutdown).
* :func:`notify_watchdog()` -- send ``WATCHDOG=1`` (call from your tick loop).
* :func:`watchdog_interval_seconds()` -- read ``$WATCHDOG_USEC`` and recommend
  half that as the ping interval.
* :class:`WatchdogPinger`   -- async helper that pings every N seconds in
  the background. Use as ``async with WatchdogPinger(): ...``.

Usage in a daemon::

    from eta_engine.obs.watchdog import (
        notify_ready, notify_stopping, WatchdogPinger,
    )

    async def main():
        notify_ready()
        async with WatchdogPinger():     # auto-pings every WATCHDOG_USEC/2
            await run_loop()
        notify_stopping()

If the daemon is run outside systemd (development, tests), ALL these
calls are no-ops -- ``$NOTIFY_SOCKET`` is unset and there's nothing to
talk to. The daemon stays runnable in either context.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

log = logging.getLogger(__name__)

_NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"
_WATCHDOG_USEC_ENV = "WATCHDOG_USEC"
_WATCHDOG_PID_ENV  = "WATCHDOG_PID"


def sd_notify(state: str) -> bool:
    """Send a single sd_notify message; return True iff a socket write happened.

    A return value of ``False`` means the daemon is not running under
    systemd notification (``$NOTIFY_SOCKET`` unset) or the socket write
    failed -- in either case the caller should NOT raise; sd_notify is
    advisory.
    """
    sock_path = os.environ.get(_NOTIFY_SOCKET_ENV, "")
    if not sock_path:
        return False
    # systemd uses '@' as a leading char to indicate an abstract namespace
    # socket; strip it and prepend NUL per AF_UNIX abstract-socket convention.
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(state.encode("utf-8"), sock_path)
    except OSError as e:
        log.debug("sd_notify failed: %s", e)
        return False
    return True


def notify_ready() -> bool:
    """Send ``READY=1`` -- transition a ``Type=notify`` unit to ``active``."""
    return sd_notify("READY=1")


def notify_stopping() -> bool:
    """Send ``STOPPING=1`` -- announce graceful shutdown."""
    return sd_notify("STOPPING=1")


def notify_watchdog() -> bool:
    """Send ``WATCHDOG=1`` -- reset the systemd watchdog timer."""
    return sd_notify("WATCHDOG=1")


def notify_status(text: str) -> bool:
    """Send ``STATUS=<text>`` -- visible in ``systemctl status``."""
    return sd_notify(f"STATUS={text}")


def watchdog_interval_seconds() -> float | None:
    """Return the recommended ping interval (half of WATCHDOG_USEC).

    Returns ``None`` if the env var is unset (no watchdog configured) or
    cannot be parsed. systemd populates the env in microseconds.
    """
    raw = os.environ.get(_WATCHDOG_USEC_ENV, "")
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    if usec <= 0:
        return None
    # Half the interval is the canonical recommendation -- gives one
    # missed ping of buffer before systemd kills.
    return usec / 2 / 1_000_000.0


class WatchdogPinger:
    """Async context manager that fires ``WATCHDOG=1`` on a fixed interval.

    Usage::

        async with WatchdogPinger():
            await run_loop()

    If no watchdog is configured, the pinger is a no-op (still safe to use,
    so callers don't need to branch on whether the env is set).
    """

    def __init__(self, interval_seconds: float | None = None) -> None:
        self._interval = interval_seconds or watchdog_interval_seconds()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> WatchdogPinger:
        if self._interval and self._interval > 0:
            self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def _loop(self) -> None:
        # Send one immediately so systemd's watchdog "first ping" deadline
        # is satisfied even on slow startups.
        notify_watchdog()
        while True:
            await asyncio.sleep(self._interval or 1.0)
            notify_watchdog()
