"""
EVOLUTIONARY TRADING ALGO  //  obs.heartbeat
================================
Heartbeat monitor -- detects silent bots and fires an alert.

Each bot registers with `register(bot_name)` at boot and calls `tick(bot_name)`
after every loop iteration. `run(interval_s)` scans every interval and alerts
via a bound MultiAlerter when any bot has been silent past `timeout_s`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from eta_engine.obs.alerts import Alert, AlertLevel, MultiAlerter


class HeartbeatMonitor:
    """In-memory bot liveness tracker with stale-detection alerts."""

    def __init__(
        self,
        alerter: MultiAlerter | None = None,
        default_timeout_s: int = 60,
    ) -> None:
        self._last: dict[str, datetime] = {}
        self._timeouts: dict[str, int] = {}
        self._alerted: set[str] = set()
        self.alerter = alerter
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
        if self.alerter is None:
            return
        for name in stale:
            if name in self._alerted:
                continue
            self._alerted.add(name)
            alert = Alert(
                level=AlertLevel.CRITICAL,
                title=f"Bot stale: {name}",
                message=f"No heartbeat from {name} within its timeout window.",
                context={"bot": name, "last_seen": str(self._last.get(name))},
                dedup_key=f"heartbeat_stale::{name}",
            )
            await self.alerter.send(alert)

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
