"""Tests for the external cloud-side watchdog kill-switch.

Covers:
  * Config env parsing
  * State classification (HEALTHY / DEGRADED / STALE)
  * Telegram + Twilio alert dispatch (with & without env creds)
  * Flatten stubs honor dry-run and missing-creds paths
  * Execute-trigger flow aggregates flatten results + alerts
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from eta_engine.scripts import watchdog_external as wd


def _config(
    dry_run: bool = False,
    kraken: bool = True,
    ibkr: bool = True,
    hyperliquid: bool = False,
    telegram: bool = True,
    sms: bool = True,
) -> wd.WatchdogConfig:
    return wd.WatchdogConfig(
        heartbeat_url="https://trading-vps.example.com/hb",
        heartbeat_timeout_s=90.0,
        poll_interval_s=15.0,
        degraded_threshold_s=45.0,
        kraken_enabled=kraken,
        ibkr_enabled=ibkr,
        hyperliquid_enabled=hyperliquid,
        telegram_enabled=telegram,
        sms_enabled=sms,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_from_env_defaults_when_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for key in list(__import__("os").environ):
        if key.startswith("WATCHDOG_"):
            monkeypatch.delenv(key, raising=False)
    config = wd.WatchdogConfig.from_env()
    assert config.heartbeat_url == ""
    assert config.heartbeat_timeout_s == 90.0
    assert config.poll_interval_s == 15.0
    assert config.kraken_enabled is True
    assert config.ibkr_enabled is True
    assert config.hyperliquid_enabled is False
    assert config.dry_run is False


def test_config_from_env_reads_dry_run(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WATCHDOG_HEARTBEAT_URL", "https://foo/hb")
    monkeypatch.setenv("WATCHDOG_DRY_RUN", "true")
    monkeypatch.setenv("WATCHDOG_HYPERLIQUID_ENABLED", "true")
    config = wd.WatchdogConfig.from_env()
    assert config.heartbeat_url == "https://foo/hb"
    assert config.dry_run is True
    assert config.hyperliquid_enabled is True


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


def test_classify_state_never_beat_is_stale() -> None:
    runtime = wd.WatchdogRuntime()
    assert wd.classify_state(_config(), runtime) is wd.WatchdogState.STALE


def test_classify_state_fresh_is_healthy() -> None:
    runtime = wd.WatchdogRuntime()
    runtime.last_heartbeat_ts = time.time() - 5.0
    assert wd.classify_state(_config(), runtime) is wd.WatchdogState.HEALTHY


def test_classify_state_mid_range_is_degraded() -> None:
    runtime = wd.WatchdogRuntime()
    runtime.last_heartbeat_ts = time.time() - 60.0  # > 45s, < 90s
    assert wd.classify_state(_config(), runtime) is wd.WatchdogState.DEGRADED


def test_classify_state_above_timeout_is_stale() -> None:
    runtime = wd.WatchdogRuntime()
    runtime.last_heartbeat_ts = time.time() - 120.0
    assert wd.classify_state(_config(), runtime) is wd.WatchdogState.STALE


# ---------------------------------------------------------------------------
# Flatten paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flatten_kraken_dry_run_returns_true() -> None:
    assert await wd.flatten_kraken_positions(_config(dry_run=True)) is True


@pytest.mark.asyncio
async def test_flatten_kraken_disabled_returns_false() -> None:
    assert await wd.flatten_kraken_positions(_config(kraken=False)) is False


@pytest.mark.asyncio
async def test_flatten_kraken_without_creds_returns_false(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("WATCHDOG_KRAKEN_KEY", raising=False)
    monkeypatch.delenv("WATCHDOG_KRAKEN_SECRET", raising=False)
    assert await wd.flatten_kraken_positions(_config()) is False


@pytest.mark.asyncio
async def test_flatten_ibkr_dry_run_returns_true() -> None:
    assert await wd.flatten_ibkr_positions(_config(dry_run=True)) is True


@pytest.mark.asyncio
async def test_flatten_ibkr_without_account_returns_false(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("WATCHDOG_IBKR_ACCOUNT_ID", raising=False)
    assert await wd.flatten_ibkr_positions(_config()) is False


@pytest.mark.asyncio
async def test_flatten_hyperliquid_disabled_by_default_returns_false() -> None:
    assert await wd.flatten_hyperliquid_positions(_config()) is False


@pytest.mark.asyncio
async def test_flatten_hyperliquid_dry_run_returns_true() -> None:
    assert (
        await wd.flatten_hyperliquid_positions(
            _config(dry_run=True, hyperliquid=True),
        )
        is True
    )


@pytest.mark.asyncio
async def test_flatten_hyperliquid_without_signer_returns_false() -> None:
    assert (
        await wd.flatten_hyperliquid_positions(
            _config(hyperliquid=True),
        )
        is False
    )


# ---------------------------------------------------------------------------
# Alert dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_alert_without_any_creds_still_increments_counter(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    for key in [
        "WATCHDOG_TELEGRAM_TOKEN",
        "WATCHDOG_TELEGRAM_CHAT_ID",
        "WATCHDOG_TWILIO_ACCOUNT_SID",
        "WATCHDOG_TWILIO_AUTH_TOKEN",
        "WATCHDOG_TWILIO_FROM",
        "WATCHDOG_SMS_TO",
    ]:
        monkeypatch.delenv(key, raising=False)

    runtime = wd.WatchdogRuntime()
    await wd.send_alert(_config(), runtime, "WARNING", "test")
    assert runtime.alerts_sent == 1


@pytest.mark.asyncio
async def test_send_alert_fires_telegram_when_configured(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    calls: list[tuple[str, str, str]] = []

    async def fake_telegram(token: str, chat: str, message: str) -> bool:
        calls.append((token, chat, message))
        return True

    monkeypatch.setenv("WATCHDOG_TELEGRAM_TOKEN", "tkn")
    monkeypatch.setenv("WATCHDOG_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(wd, "_send_telegram", fake_telegram)

    runtime = wd.WatchdogRuntime()
    await wd.send_alert(_config(sms=False), runtime, "CRITICAL", "stale")
    assert len(calls) == 1
    assert calls[0][0] == "tkn"
    assert calls[0][1] == "42"
    assert "CRITICAL" in calls[0][2]
    assert "stale" in calls[0][2]


@pytest.mark.asyncio
async def test_send_alert_skips_telegram_without_creds(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    calls: list[Any] = []

    async def fake_telegram(*args, **kwargs) -> bool:  # type: ignore[no-untyped-def]
        calls.append(1)
        return True

    monkeypatch.delenv("WATCHDOG_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("WATCHDOG_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(wd, "_send_telegram", fake_telegram)

    runtime = wd.WatchdogRuntime()
    await wd.send_alert(_config(sms=False), runtime, "WARNING", "test")
    assert calls == []  # never called


@pytest.mark.asyncio
async def test_send_alert_fires_twilio_when_configured(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    calls: list[tuple[str, str, str, str, str]] = []

    async def fake_twilio(
        sid: str,
        auth: str,
        from_n: str,
        to_n: str,
        message: str,
    ) -> bool:
        calls.append((sid, auth, from_n, to_n, message))
        return True

    monkeypatch.setenv("WATCHDOG_TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("WATCHDOG_TWILIO_AUTH_TOKEN", "sekrit")
    monkeypatch.setenv("WATCHDOG_TWILIO_FROM", "+15551110000")
    monkeypatch.setenv("WATCHDOG_SMS_TO", "+15552223333")
    monkeypatch.setattr(wd, "_send_twilio_sms", fake_twilio)

    runtime = wd.WatchdogRuntime()
    await wd.send_alert(_config(telegram=False), runtime, "CRITICAL", "flatten")
    assert len(calls) == 1
    sid, auth, from_n, to_n, message = calls[0]
    assert sid == "AC1"
    assert auth == "sekrit"
    assert from_n == "+15551110000"
    assert to_n == "+15552223333"
    assert "flatten" in message


@pytest.mark.asyncio
async def test_send_alert_fires_both_channels_when_both_configured(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    tg_calls: list[int] = []
    sms_calls: list[int] = []

    async def fake_telegram(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        tg_calls.append(1)
        return True

    async def fake_twilio(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        sms_calls.append(1)
        return True

    for k, v in [
        ("WATCHDOG_TELEGRAM_TOKEN", "tkn"),
        ("WATCHDOG_TELEGRAM_CHAT_ID", "1"),
        ("WATCHDOG_TWILIO_ACCOUNT_SID", "AC1"),
        ("WATCHDOG_TWILIO_AUTH_TOKEN", "x"),
        ("WATCHDOG_TWILIO_FROM", "+1"),
        ("WATCHDOG_SMS_TO", "+2"),
    ]:
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(wd, "_send_telegram", fake_telegram)
    monkeypatch.setattr(wd, "_send_twilio_sms", fake_twilio)

    runtime = wd.WatchdogRuntime()
    await wd.send_alert(_config(), runtime, "CRITICAL", "both")
    assert tg_calls == [1]
    assert sms_calls == [1]


# ---------------------------------------------------------------------------
# Execute-trigger end-to-end (dry-run, so no real HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_trigger_dry_run_aggregates_all_channels(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    alerts: list[tuple[str, str]] = []

    async def fake_alert(
        config: wd.WatchdogConfig,
        runtime: wd.WatchdogRuntime,
        severity: str,
        message: str,
    ) -> None:
        alerts.append((severity, message))
        runtime.alerts_sent += 1

    monkeypatch.setattr(wd, "send_alert", fake_alert)

    runtime = wd.WatchdogRuntime()
    await wd.execute_trigger(_config(dry_run=True), runtime)
    assert runtime.triggers_fired == 1
    assert runtime.state is wd.WatchdogState.TRIGGERED
    # At least: initial "firing" alert + aggregate "flatten results" alert
    assert len(alerts) >= 2
    severities = [a[0] for a in alerts]
    assert all(s == "CRITICAL" for s in severities)
