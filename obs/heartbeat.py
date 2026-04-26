"""
APEX PREDATOR  //  obs.heartbeat
================================
Heartbeat monitor -- detects silent bots and fires an alert.

Each bot registers with `register(bot_name)` at boot and calls `tick(bot_name)`
after every loop iteration. `run(interval_s)` scans every interval and alerts
via a bound MultiAlerter when any bot has been silent past `timeout_s`.

Two parallel alert paths:

* The original ``MultiAlerter`` (``alerter`` ctor arg) -- legacy, fires
  whatever transports it is configured with (Slack, console, etc.).
* An optional :class:`apex_predator.obs.alert_dispatcher.AlertDispatcher`
  (``dispatcher`` ctor arg) -- when provided, also emits a
  ``deadman_timeout`` event so the YAML-routed channels (mcc_push, etc.)
  fire on stale bots. Backwards compatible: when None, behaviour is
  identical to the legacy single-alerter path.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from apex_predator.obs.alerts import Alert, AlertLevel, MultiAlerter

if TYPE_CHECKING:
    from apex_predator.obs.alert_dispatcher import AlertDispatcher


class HeartbeatMonitor:
    """In-memory bot liveness tracker with stale-detection alerts."""

    def __init__(
        self,
        alerter: MultiAlerter | None = None,
        default_timeout_s: int = 60,
        dispatcher: AlertDispatcher | None = None,
    ) -> None:
        self._last: dict[str, datetime] = {}
        self._timeouts: dict[str, int] = {}
        self._alerted: set[str] = set()
        self.alerter = alerter
        self.dispatcher = dispatcher
        self.default_timeout_s = default_timeout_s
        self._running = False

    def register(self, bot_name: str, timeout_s: int | None = None) -> None:
        """Register a bot as alive at t=now with an optional per-bot timeout."""
        self._last[bot_name] = datetime.now(UTC)
        self._timeouts[bot_name] = timeout_s if timeout_s is not None else self.default_timeout_s
        self._alerted.discard(bot_name)

    def deregister(self, bot_name: str) -> None:
        self._last.pop(bot_name, None)
        self._timeouts.pop(bot_name, None)
        self._alerted.discard(bot_name)

    def tick(self, bot_name: str) -> None:
        """Heartbeat from a bot. Auto-registers with default timeout if new."""
        if bot_name not in self._timeouts:
            self._timeouts[bot_name] = self.default_timeout_s
        self._last[bot_name] = datetime.now(UTC)
        self._alerted.discard(bot_name)

    def check_stale(self, timeout_s: int | None = None) -> list[str]:
        """Return names of bots silent longer than their (or the given) timeout."""
        now = datetime.now(UTC)
        stale: list[str] = []
        for name, last in self._last.items():
            effective = timeout_s if timeout_s is not None else self._timeouts.get(name, self.default_timeout_s)
            if (now - last) > timedelta(seconds=effective):
                stale.append(name)
        return stale

    def last_seen(self, bot_name: str) -> datetime | None:
        return self._last.get(bot_name)

    async def _alert_stale(self, stale: list[str]) -> None:
        for name in stale:
            if name in self._alerted:
                continue
            self._alerted.add(name)
            last = self._last.get(name)
            now = datetime.now(UTC)
            stale_seconds = (now - last).total_seconds() if last is not None else None
            timeout_s = self._timeouts.get(name, self.default_timeout_s)
            # Legacy MultiAlerter path -- preserved for backwards compat.
            if self.alerter is not None:
                alert = Alert(
                    level=AlertLevel.CRITICAL,
                    title=f"Bot stale: {name}",
                    message=f"No heartbeat from {name} within its timeout window.",
                    context={"bot": name, "last_seen": str(last)},
                    dedup_key=f"heartbeat_stale::{name}",
                )
                await self.alerter.send(alert)
            # AlertDispatcher path -- routes through configs/alerts.yaml so
            # mcc_push (and any other configured channels) fire. Never
            # raises; an alert-path failure must not crash the monitor.
            if self.dispatcher is not None:
                with contextlib.suppress(Exception):
                    self.dispatcher.send(
                        "deadman_timeout",
                        {
                            "bot": name,
                            "last_heartbeat": last.isoformat() if last else None,
                            "stale_seconds": stale_seconds,
                            "timeout_seconds": timeout_s,
                        },
                    )

    async def run(self, interval_s: int = 10) -> None:
        """Loop: every `interval_s`, check for stale bots and alert."""
        self._running = True
        while self._running:
            stale = self.check_stale()
            if stale:
                await self._alert_stale(stale)
            await asyncio.sleep(interval_s)

    def stop(self) -> None:
        self._running = False
