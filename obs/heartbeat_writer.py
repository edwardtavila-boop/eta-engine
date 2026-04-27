"""Heartbeat-FILE writer for daemon recovery (Tier-3 #8 wiring, 2026-04-27).

Sibling module to ``obs/heartbeat.py``. The existing ``HeartbeatMonitor``
is an in-memory bot-liveness tracker that fires alerts; this module is
the FILE-BASED heartbeat that ``daemon_recovery_watchdog`` reads via
mtime to detect deadlocked daemons.

Long-running daemons SHOULD drop a heartbeat file every N seconds so
``daemon_recovery_watchdog`` (running every 1m) can taskkill them when
the file goes stale > 3x cadence. The avengers_daemon already does this
directly; this module gives every other daemon a standard helper that
emits the same shape so the watchdog finds them all.

Usage::

    from eta_engine.obs.heartbeat_writer import HeartbeatWriter

    hb = HeartbeatWriter("jarvis_live")
    while True:
        do_work()
        hb.tick({"cycle": n, "last_signal": "MNQ"})
        time.sleep(30)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class HeartbeatWriter:
    """Writes a heartbeat JSON file every ``tick()``.

    The watchdog reads file mtime, not content -- the JSON body is
    purely for human debugging. Reset the cycle counter by constructing
    a new instance.
    """

    def __init__(
        self,
        name: str,
        *,
        state_dir: Path | None = None,
        filename: str | None = None,
    ) -> None:
        self.name = name
        self.state_dir = state_dir or self._default_state_dir()
        self.filename = filename or f"{name}_heartbeat.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / self.filename
        self._cycle = 0

    @staticmethod
    def _default_state_dir() -> Path:
        # %LOCALAPPDATA%\eta_engine\state mirrors the avengers convention
        # so daemon_recovery_watchdog finds heartbeats in one spot.
        return Path(os.environ.get("LOCALAPPDATA", "")) / "eta_engine" / "state"

    def tick(self, extras: dict[str, Any] | None = None) -> Path:
        """Emit one heartbeat. Returns the path written for diagnostic logging."""
        self._cycle += 1
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "ts": now.timestamp(),
            "iso": now.isoformat(),
            "name": self.name,
            "pid": os.getpid(),
            "cycle": self._cycle,
        }
        if extras:
            payload.update(extras)
        try:
            self.path.write_text(json.dumps(payload, default=str), encoding="utf-8")
        except OSError as exc:
            logger.warning("heartbeat write failed for %s (%s): %s",
                           self.name, self.path, exc)
        return self.path

    def stale(self, threshold_s: float) -> bool:
        """Self-check: is the most recent heartbeat older than threshold?"""
        if not self.path.exists():
            return True
        try:
            age = (datetime.now(UTC).timestamp() - self.path.stat().st_mtime)
            return age > threshold_s
        except OSError:
            return True
