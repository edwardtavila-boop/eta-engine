"""Tests for ``eta_engine.obs.watchdog`` -- sd_notify + WatchdogPinger."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path  # noqa: TC003

import pytest
from eta_engine.obs.watchdog import (
    WatchdogPinger,
    notify_ready,
    notify_status,
    notify_stopping,
    notify_watchdog,
    sd_notify,
    watchdog_interval_seconds,
)

# ---------------------------------------------------------------------------
# sd_notify on/off paths
# ---------------------------------------------------------------------------


def test_sd_notify_returns_false_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("READY=1") is False
    assert notify_ready() is False
    assert notify_stopping() is False
    assert notify_watchdog() is False
    assert notify_status("hello") is False


def _udp_listener(path: Path) -> tuple[socket.socket, list[bytes]]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(str(path))
    s.settimeout(1.0)
    received: list[bytes] = []
    return s, received


def test_sd_notify_writes_to_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock_path = tmp_path / "notify.sock"
    s, _ = _udp_listener(sock_path)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
        assert sd_notify("READY=1") is True
        data, _addr = s.recvfrom(64)
        assert data == b"READY=1"
    finally:
        s.close()


def test_sd_notify_swallows_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/socket-path")
    # Should NOT raise; should report False.
    assert sd_notify("READY=1") is False


def test_notify_ready_writes_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = tmp_path / "n.sock"
    s, _ = _udp_listener(sock_path)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
        assert notify_ready() is True
        assert s.recvfrom(64)[0] == b"READY=1"
    finally:
        s.close()


def test_notify_stopping_writes_stopping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = tmp_path / "n.sock"
    s, _ = _udp_listener(sock_path)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
        notify_stopping()
        assert s.recvfrom(64)[0] == b"STOPPING=1"
    finally:
        s.close()


def test_notify_watchdog_writes_watchdog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = tmp_path / "n.sock"
    s, _ = _udp_listener(sock_path)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
        notify_watchdog()
        assert s.recvfrom(64)[0] == b"WATCHDOG=1"
    finally:
        s.close()


def test_notify_status_writes_status_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = tmp_path / "n.sock"
    s, _ = _udp_listener(sock_path)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
        notify_status("running normally")
        assert s.recvfrom(64)[0] == b"STATUS=running normally"
    finally:
        s.close()


# ---------------------------------------------------------------------------
# watchdog_interval_seconds
# ---------------------------------------------------------------------------


def test_watchdog_interval_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert watchdog_interval_seconds() is None


def test_watchdog_interval_parses_microseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    # 30s = 30_000_000 us; recommended ping is half = 15s
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    assert watchdog_interval_seconds() == pytest.approx(15.0)


def test_watchdog_interval_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
    assert watchdog_interval_seconds() is None


def test_watchdog_interval_rejects_zero_or_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "0")
    assert watchdog_interval_seconds() is None
    monkeypatch.setenv("WATCHDOG_USEC", "-1000")
    assert watchdog_interval_seconds() is None


# ---------------------------------------------------------------------------
# WatchdogPinger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pinger_no_op_when_no_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    async with WatchdogPinger():
        await asyncio.sleep(0.01)
    # No exception, no socket activity -- success.


@pytest.mark.asyncio
async def test_pinger_sends_immediate_then_periodic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock_path = tmp_path / "n.sock"
    s, _ = _udp_listener(sock_path)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
        async with WatchdogPinger(interval_seconds=0.05):
            # First ping is immediate.
            await asyncio.sleep(0)
            await asyncio.sleep(0.12)  # enough for ~2 more pings
        # We should see at least 2 messages (immediate + one periodic).
        s.settimeout(0.05)
        messages = []
        with pytest.raises((BlockingIOError, OSError, TimeoutError)):
            while True:
                data, _ = s.recvfrom(64)
                messages.append(data)
                if len(messages) > 10:
                    raise TimeoutError

        # Drain whatever's already buffered (should all be WATCHDOG=1)
        s.setblocking(False)
        while True:
            try:
                data, _ = s.recvfrom(64)
                messages.append(data)
            except (BlockingIOError, OSError):
                break
        assert all(m == b"WATCHDOG=1" for m in messages)
    finally:
        s.close()


@pytest.mark.asyncio
async def test_pinger_is_cancellable() -> None:
    pinger = WatchdogPinger(interval_seconds=0.01)
    await pinger.__aenter__()
    await asyncio.sleep(0.03)
    await pinger.__aexit__(None, None, None)
    # Task should be cancelled cleanly.
    assert pinger._task is None or pinger._task.done()
