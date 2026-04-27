"""Tests for obs.mobile_push."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.obs.mobile_push import (
    DEFAULT_MIN_SEVERITY,
    MobileAlert,
    MobilePushBus,
    MobileSeverity,
    PushoverChannel,
    TelegramChannel,
    _pushover_priority,
    _telegram_format,
)

if TYPE_CHECKING:
    import pytest


class RecordingChannel:
    """In-memory test double implementing the MobilePushChannel Protocol."""

    def __init__(
        self,
        *,
        name: str = "recording",
        enabled: bool = True,
        succeed: bool = True,
    ) -> None:
        self.name = name
        self.enabled = enabled
        self._succeed = succeed
        self.sent: list[MobileAlert] = []

    def send(self, alert: MobileAlert) -> bool:
        self.sent.append(alert)
        return self._succeed


def _alert(
    severity: MobileSeverity = MobileSeverity.CRITICAL,
    source: str = "kill_switch",
) -> MobileAlert:
    return MobileAlert(
        severity=severity,
        title="Breaker tripped",
        body="Drift detector fired + circuit breaker OPEN",
        source=source,
        tags=("breaker", "drift"),
    )


class TestSeverityRank:
    def test_default_min_is_critical(self):
        assert DEFAULT_MIN_SEVERITY == MobileSeverity.CRITICAL

    def test_pushover_priority_table(self):
        assert _pushover_priority(MobileSeverity.INFO) == -1
        assert _pushover_priority(MobileSeverity.WARN) == 0
        assert _pushover_priority(MobileSeverity.CRITICAL) == 1
        assert _pushover_priority(MobileSeverity.KILL) == 2


class TestTelegramFormat:
    def test_includes_title_and_body(self):
        alert = _alert()
        formatted = _telegram_format(alert)
        assert "Breaker tripped" in formatted
        assert "Drift detector fired" in formatted

    def test_severity_badge_present(self):
        for sev, token in (
            (MobileSeverity.INFO, "INFO"),
            (MobileSeverity.KILL, "KILL"),
        ):
            formatted = _telegram_format(MobileAlert(severity=sev, title="t", body="b"))
            assert token in formatted


class TestPushoverChannel:
    def test_disabled_when_missing_token(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
        ch = PushoverChannel()
        assert ch.enabled is False

    def test_enabled_when_env_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PUSHOVER_APP_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER_KEY", "user")
        ch = PushoverChannel()
        assert ch.enabled is True

    def test_send_returns_false_when_disabled(self):
        ch = PushoverChannel(token="", user_key="")
        assert ch.send(_alert()) is False


class TestTelegramChannel:
    def test_disabled_when_missing_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        ch = TelegramChannel()
        assert ch.enabled is False

    def test_enabled_when_both_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        ch = TelegramChannel()
        assert ch.enabled is True


class TestMobilePushBus:
    def test_below_min_severity_suppressed(self):
        ch = RecordingChannel()
        bus = MobilePushBus(channels=[ch], min_severity=MobileSeverity.CRITICAL)
        result = bus.publish(_alert(severity=MobileSeverity.INFO))
        assert result["suppressed"] is True
        assert ch.sent == []
        assert bus.suppressed_count == 1

    def test_above_threshold_fans_out_to_all_enabled(self):
        ch1 = RecordingChannel(name="ch1")
        ch2 = RecordingChannel(name="ch2")
        bus = MobilePushBus(channels=[ch1, ch2])
        result = bus.publish(_alert(severity=MobileSeverity.KILL))
        assert result["suppressed"] is False
        assert len(ch1.sent) == 1
        assert len(ch2.sent) == 1
        assert bus.sent_count == 2

    def test_disabled_channels_skipped(self):
        on_ch = RecordingChannel(name="on")
        off_ch = RecordingChannel(name="off", enabled=False)
        bus = MobilePushBus(channels=[on_ch, off_ch])
        result = bus.publish(_alert())
        assert result["results"]["on"] is True
        assert result["results"]["off"] is False
        assert len(on_ch.sent) == 1
        assert len(off_ch.sent) == 0

    def test_add_extends_channel_list(self):
        bus = MobilePushBus(channels=[])
        bus.add(RecordingChannel())
        assert len(bus.channels) == 1

    def test_from_env_uses_pushover_and_telegram(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        bus = MobilePushBus.from_env()
        assert any(isinstance(ch, PushoverChannel) for ch in bus.channels)
        assert any(isinstance(ch, TelegramChannel) for ch in bus.channels)

    def test_result_shape_when_no_channels(self):
        bus = MobilePushBus(channels=[])
        result = bus.publish(_alert(severity=MobileSeverity.KILL))
        assert result["suppressed"] is False
        assert result["results"] == {}
