"""Tests for obs.alerts: level ordering, dedup, and MultiAlerter fan-out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.obs.alerts import (
    Alert,
    AlertLevel,
    BaseAlerter,
    DiscordAlerter,
    MultiAlerter,
    SlackAlerter,
    TelegramAlerter,
)


class _CountingAlerter(BaseAlerter):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        if not self._should_send(alert):
            return False
        self.sent.append(alert)
        return True


def test_alert_level_ordering() -> None:
    assert AlertLevel.INFO < AlertLevel.WARN < AlertLevel.ERROR < AlertLevel.CRITICAL < AlertLevel.KILL
    assert int(AlertLevel.KILL) == 50


def test_alert_model_defaults_timestamp() -> None:
    a = Alert(level=AlertLevel.WARN, title="t", message="m")
    assert isinstance(a.timestamp, datetime)
    assert a.context == {}


@pytest.mark.asyncio
async def test_dedup_within_window_suppresses_second_send() -> None:
    alerter = _CountingAlerter()
    a = Alert(level=AlertLevel.WARN, title="t", message="m", dedup_key="k1")
    assert await alerter.send(a) is True
    assert await alerter.send(a) is False
    assert len(alerter.sent) == 1


@pytest.mark.asyncio
async def test_dedup_different_key_passes_through() -> None:
    alerter = _CountingAlerter()
    a = Alert(level=AlertLevel.WARN, title="t", message="m", dedup_key="k1")
    b = Alert(level=AlertLevel.WARN, title="t", message="m", dedup_key="k2")
    assert await alerter.send(a) is True
    assert await alerter.send(b) is True
    assert len(alerter.sent) == 2


@pytest.mark.asyncio
async def test_dedup_expires_after_window() -> None:
    alerter = _CountingAlerter()
    a = Alert(level=AlertLevel.WARN, title="t", message="m", dedup_key="k1")
    assert await alerter.send(a) is True
    # Rewind recorded last-seen past the dedup window
    alerter._recent["k1"] = datetime.now(UTC) - timedelta(seconds=600)
    assert await alerter.send(a) is True
    assert len(alerter.sent) == 2


@pytest.mark.asyncio
async def test_multi_alerter_fans_out() -> None:
    a1, a2, a3 = _CountingAlerter(), _CountingAlerter(), _CountingAlerter()
    multi = MultiAlerter([a1, a2, a3])
    alert = Alert(level=AlertLevel.CRITICAL, title="kill", message="boom")
    results = await multi.send(alert)
    assert results == [True, True, True]
    assert len(a1.sent) == len(a2.sent) == len(a3.sent) == 1


def test_telegram_format_contains_level_prefix() -> None:
    alerter = TelegramAlerter(bot_token="X", chat_id="Y")
    msg = alerter.format_message(Alert(level=AlertLevel.KILL, title="stop", message="now"))
    assert "[KILL]" in msg and "stop" in msg and "now" in msg


def test_discord_payload_has_color() -> None:
    alerter = DiscordAlerter(webhook_url="https://example/h")
    payload = alerter.format_payload(Alert(level=AlertLevel.ERROR, title="t", message="m"))
    assert payload["embeds"][0]["color"] == 0xE67E22


def test_slack_payload_has_attachment_color() -> None:
    alerter = SlackAlerter(webhook_url="https://example/h")
    payload = alerter.format_payload(Alert(level=AlertLevel.WARN, title="t", message="m"))
    assert payload["attachments"][0]["color"] == "#f1c40f"
