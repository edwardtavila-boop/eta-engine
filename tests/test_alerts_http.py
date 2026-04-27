"""
EVOLUTIONARY TRADING ALGO  //  tests.test_alerts_http
=========================================
HTTP integration tests for the aiohttp-backed Telegram/Discord/Slack alerters.
Uses an injected fake session — no network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from eta_engine.obs.alerts import (
    Alert,
    AlertLevel,
    DiscordAlerter,
    MultiAlerter,
    SlackAlerter,
    TelegramAlerter,
)


class _FakeResponse:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[_FakeResponse] = []
        self.closed = False

    def enqueue(self, status: int, body: str = "") -> None:
        self._queue.append(_FakeResponse(status, body))

    def _next(self) -> _FakeResponse:
        if self._queue:
            return self._queue.pop(0)
        return _FakeResponse(200, "")

    def post(self, url: str, data: str = "", headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append({"url": url, "data": data, "headers": headers or {}})
        return self._next()

    async def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_telegram_posts_json_with_chat_id_and_formatted_text() -> None:
    alerter = TelegramAlerter(bot_token="TOK", chat_id="CHAT-1")
    fake = _FakeSession()
    fake.enqueue(200, '{"ok":true}')
    alerter._session = fake

    a = Alert(level=AlertLevel.KILL, title="boom", message="tripped", context={"bot": "mnq"})
    assert await alerter.send(a) is True

    call = fake.calls[0]
    assert "api.telegram.org/botTOK/sendMessage" in call["url"]
    body = json.loads(call["data"])
    assert body["chat_id"] == "CHAT-1"
    assert "[KILL]" in body["text"]
    assert "tripped" in body["text"]


@pytest.mark.asyncio
async def test_telegram_returns_false_on_non_200() -> None:
    alerter = TelegramAlerter(bot_token="TOK", chat_id="X")
    fake = _FakeSession()
    fake.enqueue(401, '{"ok":false}')
    alerter._session = fake
    a = Alert(level=AlertLevel.INFO, title="t", message="m")
    assert await alerter.send(a) is False


@pytest.mark.asyncio
async def test_telegram_returns_false_when_missing_creds() -> None:
    alerter = TelegramAlerter(bot_token="", chat_id="")
    a = Alert(level=AlertLevel.INFO, title="t", message="m")
    # No session created, no HTTP attempted.
    assert await alerter.send(a) is False


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_discord_accepts_204() -> None:
    alerter = DiscordAlerter(webhook_url="https://discord.com/api/webhooks/hook")
    fake = _FakeSession()
    fake.enqueue(204, "")
    alerter._session = fake
    a = Alert(level=AlertLevel.CRITICAL, title="kill", message="down")
    assert await alerter.send(a) is True
    body = json.loads(fake.calls[0]["data"])
    assert body["embeds"][0]["color"] == 0xE74C3C
    assert body["username"] == "EVOLUTIONARY TRADING ALGO"


@pytest.mark.asyncio
async def test_discord_rejects_other_status() -> None:
    alerter = DiscordAlerter(webhook_url="https://x")
    fake = _FakeSession()
    fake.enqueue(400, '{"message":"invalid"}')
    alerter._session = fake
    assert await alerter.send(Alert(level=AlertLevel.WARN, title="t", message="m")) is False


@pytest.mark.asyncio
async def test_discord_no_webhook_returns_false() -> None:
    alerter = DiscordAlerter(webhook_url="")
    assert await alerter.send(Alert(level=AlertLevel.INFO, title="t", message="m")) is False


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_slack_posts_attachment_with_color() -> None:
    alerter = SlackAlerter(webhook_url="https://hooks.slack.com/X", channel="#alerts")
    fake = _FakeSession()
    fake.enqueue(200, "ok")
    alerter._session = fake
    a = Alert(level=AlertLevel.WARN, title="warn", message="soft", context={"bucket": "ETH"})
    assert await alerter.send(a) is True
    body = json.loads(fake.calls[0]["data"])
    assert body["channel"] == "#alerts"
    att = body["attachments"][0]
    assert att["color"] == "#f1c40f"
    assert att["text"] == "soft"
    assert len(att["fields"]) == 1
    assert att["fields"][0]["title"] == "bucket"


# --------------------------------------------------------------------------- #
# Dedup + MultiAlerter close
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dedup_suppresses_before_http() -> None:
    alerter = TelegramAlerter(bot_token="TOK", chat_id="X")
    fake = _FakeSession()
    fake.enqueue(200)
    alerter._session = fake
    a = Alert(level=AlertLevel.INFO, title="t", message="m", dedup_key="same")
    assert await alerter.send(a) is True
    assert await alerter.send(a) is False  # deduped
    # Only one HTTP call made
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_multi_alerter_close_closes_all_sessions() -> None:
    a1 = TelegramAlerter(bot_token="T", chat_id="C")
    a2 = DiscordAlerter(webhook_url="https://x")
    f1, f2 = _FakeSession(), _FakeSession()
    a1._session, a2._session = f1, f2
    m = MultiAlerter([a1, a2])
    await m.close()
    assert f1.closed is True and f2.closed is True
    assert a1._session is None and a2._session is None


@pytest.mark.asyncio
async def test_multi_alerter_fan_out_with_mocked_transports() -> None:
    """Verify MultiAlerter.send actually invokes HTTP per alerter."""
    a1 = TelegramAlerter(bot_token="T", chat_id="C")
    a2 = SlackAlerter(webhook_url="https://hooks")
    f1, f2 = _FakeSession(), _FakeSession()
    f1.enqueue(200)
    f2.enqueue(200)
    a1._session, a2._session = f1, f2
    m = MultiAlerter([a1, a2])
    results = await m.send(Alert(level=AlertLevel.ERROR, title="t", message="m"))
    assert results == [True, True]
    assert len(f1.calls) == 1 and len(f2.calls) == 1
