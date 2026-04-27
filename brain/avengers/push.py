"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.push
======================================
Push-notification fan-out for JARVIS alerts.

Why this exists
---------------
The daemons run 24/7 on a headless VPS. Without push channels, a critical
event (daemon crash, circuit-breaker trip, dead-man's switch engaged)
only surfaces on next login. This module provides a single ``push()``
entry point that fans out to any configured channel.

Design
------
* ``Notifier`` Protocol -- any object with ``send(level, title, body)``.
* ``LocalFileNotifier`` -- default, appends to ``~/.jarvis/alerts.jsonl``.
  Zero-config, always works, acts as audit log regardless of other channels.
* ``PushoverNotifier`` / ``TelegramNotifier`` -- optional, env-var-gated
  (PUSHOVER_TOKEN + PUSHOVER_USER, TELEGRAM_TOKEN + TELEGRAM_CHAT).
* ``push(level, title, body)`` -- top-level convenience that writes to the
  local file AND best-effort sends to any configured remote channel.

No network I/O by default. A remote notifier that raises is caught, the
failure is logged to the local file, and the loop continues.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Sequence


logger = logging.getLogger(__name__)


# Default local log sits beside the avengers journal so the operator
# can ``tail -F`` both at once.
ALERTS_JOURNAL: Path = Path.home() / ".jarvis" / "alerts.jsonl"


class AlertLevel(StrEnum):
    """Severity tiers. Maps to push priority in remote channels."""

    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class Alert(BaseModel):
    """One push payload. Serialized JSONL-style to the alerts log."""

    model_config = ConfigDict(frozen=True)

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    level: AlertLevel
    title: str = Field(min_length=1)
    body: str = ""
    source: str = "jarvis"
    tags: list[str] = Field(default_factory=list)


@runtime_checkable
class Notifier(Protocol):
    def send(self, alert: Alert) -> bool: ...


class LocalFileNotifier:
    """Always-on local audit channel. JSONL append, no network."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or ALERTS_JOURNAL

    def send(self, alert: Alert) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(alert.model_dump(mode="json"), default=str) + "\n")
        except OSError as exc:
            logger.warning("LocalFileNotifier failed: %s", exc)
            return False
        return True


class PushoverNotifier:
    """Pushover API. Env vars PUSHOVER_TOKEN + PUSHOVER_USER."""

    def __init__(self, token: str | None = None, user: str | None = None) -> None:
        self.token = token or os.environ.get("PUSHOVER_TOKEN", "")
        self.user = user or os.environ.get("PUSHOVER_USER", "")

    def configured(self) -> bool:
        return bool(self.token and self.user)

    def send(self, alert: Alert) -> bool:
        if not self.configured():
            return False
        # Lazy import so the module loads even if ``requests`` isn't present.
        try:
            import urllib.request

            priority = {
                AlertLevel.INFO: -1,
                AlertLevel.WARN: 0,
                AlertLevel.CRITICAL: 1,
            }[alert.level]
            data = (
                f"token={self.token}&user={self.user}&title={alert.title}&message={alert.body}&priority={priority}"
            ).encode()
            req = urllib.request.Request(  # noqa: S310 -- fixed https URL
                "https://api.pushover.net/1/messages.json",
                data=data,
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                return resp.status == 200
        except Exception as exc:  # noqa: BLE001 -- never crash caller
            logger.warning("PushoverNotifier failed: %s", exc)
            return False


class TelegramNotifier:
    """Telegram bot API. Env vars TELEGRAM_TOKEN + TELEGRAM_CHAT."""

    def __init__(self, token: str | None = None, chat: str | None = None) -> None:
        self.token = token or os.environ.get("TELEGRAM_TOKEN", "")
        self.chat = chat or os.environ.get("TELEGRAM_CHAT", "")

    def configured(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, alert: Alert) -> bool:
        if not self.configured():
            return False
        try:
            import urllib.parse
            import urllib.request

            text = f"[{alert.level.value}] {alert.title}\n{alert.body}"
            params = urllib.parse.urlencode(
                {
                    "chat_id": self.chat,
                    "text": text,
                }
            ).encode("utf-8")
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            with urllib.request.urlopen(  # noqa: S310 -- fixed https URL
                url,
                data=params,
                timeout=5,
            ) as resp:
                return resp.status == 200
        except Exception as exc:  # noqa: BLE001 -- never crash caller
            logger.warning("TelegramNotifier failed: %s", exc)
            return False


class PushBus:
    """Aggregates multiple notifiers. ``send`` never raises.

    Time-based deduplication
    ------------------------
    A cron task that fails every 5 minutes would otherwise spam Telegram
    every tick. ``dedup_window_seconds`` suppresses repeat ``(level,
    title, source)`` tuples within that window -- the alert is still
    appended to the local-file audit log (so you have a record), but
    the remote notifiers are skipped. ``CRITICAL`` alerts always break
    through dedup so a kill-switch trip is never silent.

    Set ``dedup_window_seconds=0`` to disable.
    """

    def __init__(
        self,
        notifiers: Sequence[Notifier] | None = None,
        *,
        dedup_window_seconds: float = 600.0,
    ) -> None:
        self._notifiers: list[Notifier] = (
            list(notifiers)
            if notifiers
            else [
                LocalFileNotifier(),
            ]
        )
        self.dedup_window_seconds = max(0.0, dedup_window_seconds)
        # (level, title, source) -> last_dispatch_ts (UTC-aware)
        self._last_seen: dict[tuple[str, str, str], datetime] = {}

    def add(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    def _is_duplicate(self, alert: Alert) -> bool:
        """Return True if this alert is a cheap repeat inside the window."""
        if self.dedup_window_seconds <= 0.0:
            return False
        if alert.level is AlertLevel.CRITICAL:
            # Kill-switch / breaker trips must always fan out.
            return False
        key = (alert.level.value, alert.title, alert.source)
        last = self._last_seen.get(key)
        now = alert.ts
        if last is not None and (now - last).total_seconds() < self.dedup_window_seconds:
            return True
        self._last_seen[key] = now
        return False

    def push(
        self,
        level: AlertLevel,
        title: str,
        body: str = "",
        *,
        source: str = "jarvis",
        tags: list[str] | None = None,
    ) -> dict[str, bool]:
        alert = Alert(
            level=level,
            title=title,
            body=body,
            source=source,
            tags=tags or [],
        )
        dup = self._is_duplicate(alert)
        results: dict[str, bool] = {}
        for n in self._notifiers:
            name = type(n).__name__
            # The local audit channel always records every alert so the
            # forensic trail is complete even under heavy dedup. Remote
            # notifiers are skipped for dupes to spare the API quota.
            is_local = isinstance(n, LocalFileNotifier)
            if dup and not is_local:
                results[name] = False
                continue
            try:
                results[name] = bool(n.send(alert))
            except Exception as exc:  # noqa: BLE001 -- keep fanning out
                logger.warning("%s raised in push(): %s", name, exc)
                results[name] = False
        return results


# Lazily-constructed module-level bus. Tests reset via ``set_default_bus``.
_default_bus: PushBus | None = None


def default_bus() -> PushBus:
    """Return a process-wide PushBus. Adds remote channels if env vars exist."""
    global _default_bus  # noqa: PLW0603 -- intentional module-level cache
    if _default_bus is None:
        bus = PushBus([LocalFileNotifier()])
        po = PushoverNotifier()
        if po.configured():
            bus.add(po)
        tg = TelegramNotifier()
        if tg.configured():
            bus.add(tg)
        _default_bus = bus
    return _default_bus


def set_default_bus(bus: PushBus | None) -> None:
    """Test hook: swap the module-level bus."""
    global _default_bus  # noqa: PLW0603
    _default_bus = bus


def push(
    level: AlertLevel,
    title: str,
    body: str = "",
    *,
    source: str = "jarvis",
    tags: list[str] | None = None,
) -> dict[str, bool]:
    """Top-level convenience: fire through the default bus."""
    return default_bus().push(
        level,
        title,
        body,
        source=source,
        tags=tags,
    )


__all__ = [
    "ALERTS_JOURNAL",
    "Alert",
    "AlertLevel",
    "LocalFileNotifier",
    "Notifier",
    "PushBus",
    "PushoverNotifier",
    "TelegramNotifier",
    "default_bus",
    "push",
    "set_default_bus",
]
