"""EVOLUTIONARY TRADING ALGO  //  obs.mobile_push.

Mobile-push channel for kill-switch / breaker / drift events.

Why this module exists
----------------------
:mod:`obs.alert_dispatcher` already routes alerts to the local journal,
Slack webhook, and email. But the operator is mobile-first — when the
kill switch fires, a desktop notification is useless if the laptop is
shut. This module adds a mobile push channel (Pushover + Telegram) with
a clean adapter seam so the alert dispatcher can fan-out to mobile
without caring about transport.

Design
------
* **Provider-agnostic.** The ``MobilePushChannel`` Protocol defines the
  shape; :class:`PushoverChannel` and :class:`TelegramChannel` are the
  two shipped implementations.
* **Env-driven config.** Both channels read credentials from env vars
  and stay dormant (``enabled=False``) when credentials are absent.
* **Severity gating.** Only ``CRITICAL`` and ``KILL`` events go to
  mobile by default — the operator does not want to be woken up by
  INFO-level chatter.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = [
    "MobileSeverity",
    "MobileAlert",
    "MobilePushChannel",
    "PushoverChannel",
    "TelegramChannel",
    "MobilePushBus",
    "DEFAULT_MIN_SEVERITY",
]


class MobileSeverity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"
    KILL = "KILL"


DEFAULT_MIN_SEVERITY: MobileSeverity = MobileSeverity.CRITICAL


_SEVERITY_RANK: dict[MobileSeverity, int] = {
    MobileSeverity.INFO: 0,
    MobileSeverity.WARN: 1,
    MobileSeverity.CRITICAL: 2,
    MobileSeverity.KILL: 3,
}


@dataclass(frozen=True)
class MobileAlert:
    """One alert destined for mobile fan-out."""

    severity: MobileSeverity
    title: str
    body: str
    source: str = "apex"  # e.g. "kill_switch", "pnl_drift", "breaker"
    ts_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    url: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


class MobilePushChannel(Protocol):
    """Any mobile transport must implement this minimal surface."""

    name: str
    enabled: bool

    def send(self, alert: MobileAlert) -> bool: ...


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------


class PushoverChannel:
    """Send alerts through the Pushover API."""

    name: str = "pushover"
    API_URL: str = "https://api.pushover.net/1/messages.json"

    def __init__(
        self,
        *,
        token: str | None = None,
        user_key: str | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self.token = token or os.getenv("PUSHOVER_APP_TOKEN", "").strip()
        self.user_key = user_key or os.getenv("PUSHOVER_USER_KEY", "").strip()
        self.timeout_s = max(1.0, float(timeout_s))

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.user_key)

    def send(self, alert: MobileAlert) -> bool:
        if not self.enabled:
            log.debug("pushover disabled — missing credentials; alert dropped")
            return False
        priority = _pushover_priority(alert.severity)
        params = {
            "token": self.token,
            "user": self.user_key,
            "title": alert.title[:250],
            "message": alert.body[:1024],
            "priority": priority,
        }
        if priority == 2:
            # Emergency priority requires retry + expire parameters
            params["retry"] = 60
            params["expire"] = 1800
        if alert.url:
            params["url"] = alert.url
        try:
            body = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request(self.API_URL, data=body)
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status_code = getattr(resp, "status", 200)
            return 200 <= status_code < 300
        except Exception as exc:  # noqa: BLE001
            log.warning("pushover send failed: %s", exc)
            return False


def _pushover_priority(severity: MobileSeverity) -> int:
    return {
        MobileSeverity.INFO: -1,
        MobileSeverity.WARN: 0,
        MobileSeverity.CRITICAL: 1,
        MobileSeverity.KILL: 2,
    }[severity]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class TelegramChannel:
    """Send alerts through the Telegram Bot API."""

    name: str = "telegram"

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.timeout_s = max(1.0, float(timeout_s))

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, alert: MobileAlert) -> bool:
        if not self.enabled:
            log.debug("telegram disabled — missing credentials; alert dropped")
            return False
        text = _telegram_format(alert)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return 200 <= getattr(resp, "status", 200) < 300
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram send failed: %s", exc)
            return False


def _telegram_format(alert: MobileAlert) -> str:
    badge = {
        MobileSeverity.INFO: "[INFO]",
        MobileSeverity.WARN: "[WARN]",
        MobileSeverity.CRITICAL: "[!!]",
        MobileSeverity.KILL: "[KILL]",
    }[alert.severity]
    tags = " ".join(f"#{t}" for t in alert.tags)
    link = f'\n<a href="{alert.url}">open</a>' if alert.url else ""
    return (f"<b>{badge} {alert.source}</b>\n<b>{alert.title}</b>\n{alert.body}\n{tags}{link}").strip()


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------


class MobilePushBus:
    """Fan-out a :class:`MobileAlert` to every enabled channel above threshold."""

    def __init__(
        self,
        channels: list[MobilePushChannel] | None = None,
        *,
        min_severity: MobileSeverity = DEFAULT_MIN_SEVERITY,
    ) -> None:
        self.channels: list[MobilePushChannel] = channels or []
        self.min_severity = min_severity
        self._sent_count = 0
        self._suppressed_count = 0

    @classmethod
    def from_env(cls, *, min_severity: MobileSeverity = DEFAULT_MIN_SEVERITY) -> MobilePushBus:
        return cls(
            channels=[PushoverChannel(), TelegramChannel()],
            min_severity=min_severity,
        )

    def add(self, channel: MobilePushChannel) -> None:
        self.channels.append(channel)

    def publish(self, alert: MobileAlert) -> dict[str, Any]:
        """Send the alert to every enabled channel. Return per-channel results."""
        if _SEVERITY_RANK[alert.severity] < _SEVERITY_RANK[self.min_severity]:
            self._suppressed_count += 1
            return {"suppressed": True, "reason": "below min_severity", "results": {}}
        results: dict[str, bool] = {}
        for ch in self.channels:
            if not ch.enabled:
                results[ch.name] = False
                continue
            ok = ch.send(alert)
            results[ch.name] = ok
            if ok:
                self._sent_count += 1
        return {
            "suppressed": False,
            "sent_count": self._sent_count,
            "results": results,
        }

    @property
    def sent_count(self) -> int:
        return self._sent_count

    @property
    def suppressed_count(self) -> int:
        return self._suppressed_count
