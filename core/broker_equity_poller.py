"""
EVOLUTIONARY TRADING ALGO  //  core.broker_equity_poller
============================================
R1 closure (wiring) -- async polling bridge between a venue adapter's
``async def get_net_liquidation()`` and the sync-callable contract that
:class:`BrokerEquityReconciler` expects for ``broker_equity_source``.

Why this module exists
----------------------
``BrokerEquityReconciler`` is invoked from the sync path of the trailing
DD runtime (one reconcile tick per live-mode cycle). Broker adapters
expose balance reads as async coroutines. We need a small shim that:

  * Runs a background poller coroutine on the current event loop
  * Caches the latest successful value
  * Exposes a synchronous ``current()`` method returning ``float | None``
  * Marks the cache stale when the backend has not responded for longer
    than ``stale_after_s`` -- a stale value is worse than no data because
    it can silently hide real drift

Usage
-----
    tasty = TastytradeVenue()
    poller = BrokerEquityPoller(
        name="tastytrade",
        fetch_fn=tasty.get_net_liquidation,
        refresh_s=5.0,
        stale_after_s=30.0,
    )
    await poller.start()
    rec = BrokerEquityReconciler(broker_equity_source=poller.current)
    ...
    await poller.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class BrokerEquityPoller:
    """Async polling wrapper around an adapter's net-liquidation reader.

    Parameters
    ----------
    name:
        Identifier used in log lines (e.g. ``"tastytrade"``, ``"ibkr"``).
    fetch_fn:
        Zero-arg async callable returning broker net-liq USD or ``None``.
        Typically ``adapter.get_net_liquidation``.
    refresh_s:
        Poll interval in seconds. Default ``5.0``. The poller waits
        ``refresh_s`` between successful fetches; a raised exception or
        ``None`` result still advances the interval (we don't hot-spin).
    stale_after_s:
        How old a cached value can be before ``current()`` returns
        ``None``. Guards against a frozen adapter silently serving a
        stale MTM to the reconciler. Default ``30.0``.
    """

    def __init__(
        self,
        *,
        name: str,
        fetch_fn: Callable[[], Awaitable[float | None]],
        refresh_s: float = 5.0,
        stale_after_s: float = 30.0,
        identical_warn_after: int = 0,
    ) -> None:
        if refresh_s <= 0:
            msg = f"refresh_s must be > 0 (got {refresh_s})"
            raise ValueError(msg)
        if stale_after_s <= 0:
            msg = f"stale_after_s must be > 0 (got {stale_after_s})"
            raise ValueError(msg)
        if identical_warn_after < 0:
            msg = f"identical_warn_after must be >= 0 (got {identical_warn_after})"
            raise ValueError(msg)
        self.name = name
        self._fetch = fetch_fn
        self._refresh_s = float(refresh_s)
        self._stale_after_s = float(stale_after_s)
        # H4 partial closure (v0.1.69 -- byte-identical detection track).
        # The Red Team finding observed that ``stale_after_s`` measures
        # OUR poll-cycle age, not the broker's snapshot age. A broker
        # that returns the same cached value for many polls in a row
        # (server-side staleness, IBKR Client Portal session frozen,
        # Tastytrade balances endpoint glued to a stale snapshot) would
        # never trip the existing TTL guard. ``identical_warn_after``
        # counts consecutive identical poll values and emits a WARN log
        # when the count crosses the threshold. Observation-only --
        # ``current()`` keeps returning the cached value because in
        # quiet markets identical polls are NORMAL (a flat balance with
        # no trades produces them). The operator can grep
        # ``broker_equity_poller_identical_polls`` and triage. A future
        # H4 deepening (``v0.1.70+``: lands when the venue adapters
        # expose a server-side timestamp via the protocol -- IBKR
        # ``/iserver/account/pnl/partitioned`` and Tastytrade balances
        # both ship one) replaces this heuristic with a hard staleness
        # check on the broker's own snapshot timestamp.
        # Default 0 = disabled (back-compat). Recommended: 5+ polls.
        self._identical_warn_after = int(identical_warn_after)
        self._last_value: float | None = None
        self._last_success_ts: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # Counters for observability. Exposed read-only via properties.
        self._fetch_ok = 0
        self._fetch_none = 0
        self._fetch_error = 0
        self._consecutive_identical = 0

    @property
    def fetch_ok(self) -> int:
        return self._fetch_ok

    @property
    def fetch_none(self) -> int:
        return self._fetch_none

    @property
    def fetch_error(self) -> int:
        return self._fetch_error

    @property
    def last_success_ts(self) -> datetime | None:
        return self._last_success_ts

    @property
    def consecutive_identical(self) -> int:
        """Count of consecutive successful polls returning the SAME value.

        Reset to 0 every time the broker's reported value changes. Useful
        in dashboards / nightly audits as an early-warning signal that
        the broker may be serving a server-side cached snapshot.
        """
        return self._consecutive_identical

    def is_running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    async def start(self) -> None:
        """Kick off the background poll loop on the current event loop."""
        if self.is_running():
            return
        self._stopping.clear()
        # Do one eager fetch so ``current()`` has data immediately after
        # ``start()`` returns -- supervisors otherwise would race with
        # the first ``refresh_s`` interval.
        await self._poll_once()
        self._task = asyncio.create_task(
            self._loop(),
            name=f"broker-equity-poller:{self.name}",
        )

    async def stop(self) -> None:
        """Signal the loop to exit and wait for the task to finish."""
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=self._refresh_s + 2.0)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def current(self) -> float | None:
        """Return the cached net-liq, or ``None`` if stale / never fetched.

        This is the ``broker_equity_source`` callable consumed by
        :class:`BrokerEquityReconciler`.
        """
        if self._last_value is None or self._last_success_ts is None:
            return None
        age_s = (datetime.now(UTC) - self._last_success_ts).total_seconds()
        if age_s > self._stale_after_s:
            log.debug(
                "%s: cached broker equity stale (%.1fs > %.1fs) -- returning None",
                self.name,
                age_s,
                self._stale_after_s,
            )
            return None
        return self._last_value

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._refresh_s,
                )
            if self._stopping.is_set():
                return
            await self._poll_once()

    async def _poll_once(self) -> None:
        try:
            value = await self._fetch()
        except Exception as exc:  # noqa: BLE001
            self._fetch_error += 1
            log.warning(
                "%s: broker equity fetch raised %s",
                self.name,
                exc,
                exc_info=True,
            )
            return
        if value is None:
            self._fetch_none += 1
            return
        new_value = float(value)
        # H4 partial: track consecutive-identical run length. A change
        # resets the counter to 0; an identical value bumps it. We
        # compare on the cached float, not the raw response, so a
        # single noise byte in a JSON timestamp field doesn't make us
        # think the snapshot moved when net-liq itself didn't.
        if self._last_value is not None and new_value == self._last_value:
            self._consecutive_identical += 1
            if self._identical_warn_after > 0 and self._consecutive_identical == self._identical_warn_after:
                # Fire the warn exactly once at the boundary (==, not >=)
                # so a sustained-identical condition does not log-spam.
                log.warning(
                    "%s: %d consecutive identical broker net-liq polls "
                    "(value=%.2f). Quiet market is normal; if this is "
                    "active hours the broker may be serving a stale "
                    "snapshot. Check with: python -m eta_engine."
                    "scripts.connect_brokers --probe",
                    self.name,
                    self._consecutive_identical,
                    new_value,
                )
        else:
            self._consecutive_identical = 0
        self._last_value = new_value
        self._last_success_ts = datetime.now(UTC)
        self._fetch_ok += 1


__all__ = ["BrokerEquityPoller"]
