"""
EVOLUTIONARY TRADING ALGO  //  obs.alerts
=============================
Alerting fan-out: Telegram / Discord / Slack with dedup.

Real aiohttp transports wired. All three send JSON payloads via POST to their
respective webhook endpoints. Each alerter handles its own dedup + session
lifecycle. Call `await alerter.close()` to release the session on shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from asyncio import gather
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEDUP_WINDOW_SECONDS = 300  # 5 minutes
_HTTP_TIMEOUT_S = 10.0
_HTTP_RETRY = 1


class AlertLevel(IntEnum):
    INFO = 10
    WARN = 20
    ERROR = 30
    CRITICAL = 40
    KILL = 50


_LEVEL_PREFIX: dict[AlertLevel, str] = {
    AlertLevel.INFO: "[INFO]",
    AlertLevel.WARN: "[WARN]",
    AlertLevel.ERROR: "[ERR]",
    AlertLevel.CRITICAL: "[CRIT]",
    AlertLevel.KILL: "[KILL]",
}

# Discord embed colors (decimal)
_DISCORD_COLOR: dict[AlertLevel, int] = {
    AlertLevel.INFO: 0x3498DB,
    AlertLevel.WARN: 0xF1C40F,
    AlertLevel.ERROR: 0xE67E22,
    AlertLevel.CRITICAL: 0xE74C3C,
    AlertLevel.KILL: 0x000000,
}

# Slack attachment color (hex w/ leading '#')
_SLACK_COLOR: dict[AlertLevel, str] = {
    AlertLevel.INFO: "#3498db",
    AlertLevel.WARN: "#f1c40f",
    AlertLevel.ERROR: "#e67e22",
    AlertLevel.CRITICAL: "#e74c3c",
    AlertLevel.KILL: "#000000",
}


class Alert(BaseModel):
    """An outgoing alert record."""

    level: AlertLevel
    title: str
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    context: dict[str, str] = Field(default_factory=dict)
    dedup_key: str | None = None


class BaseAlerter(ABC):
    """Abstract alerter with dedup on (dedup_key, 5-min window)."""

    def __init__(self) -> None:
        self._recent: dict[str, datetime] = {}
        self._session: Any = None  # aiohttp.ClientSession, lazy

    def _should_send(self, alert: Alert) -> bool:
        if alert.dedup_key is None:
            return True
        last = self._recent.get(alert.dedup_key)
        now = datetime.now(UTC)
        if last is not None and (now - last) < timedelta(seconds=DEDUP_WINDOW_SECONDS):
            return False
        self._recent[alert.dedup_key] = now
        return True

    async def _ensure_session(self) -> Any:  # noqa: ANN401 - aiohttp imported lazily; real type is aiohttp.ClientSession
        if self._session is None:
            import aiohttp  # noqa: PLC0415

            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("alerter.close session close raised %s", e)
            self._session = None

    async def _http_post_json(self, url: str, payload: dict[str, Any]) -> tuple[int, str]:
        """POST JSON. Single retry on transient ClientError. Returns (status, body)."""
        session = await self._ensure_session()
        body = json.dumps(payload)
        headers = {"Content-Type": "application/json"}
        last_exc: Exception | None = None
        for attempt in range(_HTTP_RETRY + 1):
            try:
                async with session.post(url, data=body, headers=headers) as resp:
                    txt = await resp.text()
                    return resp.status, txt
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("alerter POST attempt=%d failed: %s", attempt + 1, exc)
                if attempt >= _HTTP_RETRY:
                    break
        assert last_exc is not None
        raise last_exc

    @abstractmethod
    async def send(self, alert: Alert) -> bool:
        """Deliver alert to the transport. Return True if sent, False if deduped/suppressed."""


class TelegramAlerter(BaseAlerter):
    """Telegram Bot API alerter."""

    def __init__(self, bot_token: str, chat_id: str, parse_mode: str = "Markdown") -> None:
        super().__init__()
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        self.endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def format_message(self, alert: Alert) -> str:
        prefix = _LEVEL_PREFIX[alert.level]
        ctx = ""
        if alert.context:
            ctx = "\n" + "\n".join(f"- *{k}*: `{v}`" for k, v in alert.context.items())
        return f"{prefix} *{alert.title}*\n{alert.message}{ctx}"

    async def send(self, alert: Alert) -> bool:
        if not self._should_send(alert):
            return False
        if not self.bot_token or not self.chat_id:
            # Missing creds -> log-only, don't raise (safe no-op for tests/dryrun)
            logger.info("telegram skip (no token/chat_id): %s", self.format_message(alert))
            return False
        payload = {
            "chat_id": self.chat_id,
            "text": self.format_message(alert),
            "parse_mode": self.parse_mode,
        }
        try:
            status, body = await self._http_post_json(self.endpoint, payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("telegram send raised: %s", exc)
            return False
        if status != 200:
            logger.error("telegram send failed status=%s body=%s", status, body[:500])
            return False
        return True


class DiscordAlerter(BaseAlerter):
    """Discord webhook alerter with embed."""

    def __init__(self, webhook_url: str, username: str = "EVOLUTIONARY TRADING ALGO") -> None:
        super().__init__()
        self.webhook_url = webhook_url
        self.username = username

    def format_payload(self, alert: Alert) -> dict:
        fields = [{"name": k, "value": str(v), "inline": True} for k, v in alert.context.items()]
        return {
            "username": self.username,
            "embeds": [
                {
                    "title": f"{_LEVEL_PREFIX[alert.level]} {alert.title}",
                    "description": alert.message,
                    "color": _DISCORD_COLOR[alert.level],
                    "timestamp": alert.timestamp.isoformat(),
                    "fields": fields,
                }
            ],
        }

    async def send(self, alert: Alert) -> bool:
        if not self._should_send(alert):
            return False
        if not self.webhook_url:
            logger.info("discord skip (no webhook_url)")
            return False
        try:
            status, body = await self._http_post_json(self.webhook_url, self.format_payload(alert))
        except Exception as exc:  # noqa: BLE001
            logger.error("discord send raised: %s", exc)
            return False
        # Discord webhooks return 204 No Content on success.
        if status not in (200, 204):
            logger.error("discord send failed status=%s body=%s", status, body[:500])
            return False
        return True


class SlackAlerter(BaseAlerter):
    """Slack incoming-webhook alerter with colored attachment."""

    def __init__(self, webhook_url: str, channel: str | None = None) -> None:
        super().__init__()
        self.webhook_url = webhook_url
        self.channel = channel

    def format_payload(self, alert: Alert) -> dict:
        fields = [{"title": k, "value": str(v), "short": True} for k, v in alert.context.items()]
        attachment = {
            "color": _SLACK_COLOR[alert.level],
            "title": f"{_LEVEL_PREFIX[alert.level]} {alert.title}",
            "text": alert.message,
            "ts": int(alert.timestamp.timestamp()),
            "fields": fields,
        }
        payload: dict = {"attachments": [attachment]}
        if self.channel:
            payload["channel"] = self.channel
        return payload

    async def send(self, alert: Alert) -> bool:
        if not self._should_send(alert):
            return False
        if not self.webhook_url:
            logger.info("slack skip (no webhook_url)")
            return False
        try:
            status, body = await self._http_post_json(self.webhook_url, self.format_payload(alert))
        except Exception as exc:  # noqa: BLE001
            logger.error("slack send raised: %s", exc)
            return False
        if status != 200:
            logger.error("slack send failed status=%s body=%s", status, body[:500])
            return False
        return True


class MultiAlerter:
    """Fan out a single alert to many alerters, awaiting all concurrently."""

    def __init__(self, alerters: list[BaseAlerter]) -> None:
        self.alerters = alerters

    def add(self, alerter: BaseAlerter) -> None:
        self.alerters.append(alerter)

    async def send(self, alert: Alert) -> list[bool]:
        if not self.alerters:
            return []
        return list(await gather(*(a.send(alert) for a in self.alerters)))

    async def close(self) -> None:
        """Close every alerter's HTTP session. Call on shutdown."""
        if not self.alerters:
            return
        await gather(*(a.close() for a in self.alerters), return_exceptions=True)
