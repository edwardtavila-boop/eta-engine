"""Tests for obs.heartbeat: register, tick, stale detection, alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eta_engine.obs.alerts import Alert, BaseAlerter, MultiAlerter
from eta_engine.obs.heartbeat import HeartbeatMonitor


class _CapturingAlerter(BaseAlerter):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        if not self._should_send(alert):
            return False
        self.sent.append(alert)
        return True


def test_register_and_tick_updates_last_seen() -> None:
    hb = HeartbeatMonitor()
    hb.register("mnq")
    t0 = hb.last_seen("mnq")
    assert t0 is not None
    hb.tick("mnq")
    t1 = hb.last_seen("mnq")
    assert t1 is not None and t1 >= t0


def test_check_stale_returns_names_past_timeout() -> None:
    hb = HeartbeatMonitor(default_timeout_s=30)
    hb.register("mnq")
    hb.register("eth_perp")
    # Rewind mnq heartbeat into the past
    hb._last["mnq"] = datetime.now(UTC) - timedelta(seconds=120)
    stale = hb.check_stale()
    assert "mnq" in stale
    assert "eth_perp" not in stale


def test_check_stale_with_override_timeout() -> None:
    hb = HeartbeatMonitor(default_timeout_s=300)
    hb.register("mnq")
    hb._last["mnq"] = datetime.now(UTC) - timedelta(seconds=45)
    assert hb.check_stale() == []
    assert hb.check_stale(timeout_s=30) == ["mnq"]


@pytest.mark.asyncio
async def test_alerts_fire_on_stale() -> None:
    captor = _CapturingAlerter()
    multi = MultiAlerter([captor])
    hb = HeartbeatMonitor(alerter=multi, default_timeout_s=30)
    hb.register("mnq")
    hb._last["mnq"] = datetime.now(UTC) - timedelta(seconds=120)
    await hb._alert_stale(hb.check_stale())
    assert len(captor.sent) == 1
    assert "mnq" in captor.sent[0].title


@pytest.mark.asyncio
async def test_alert_not_repeated_without_recovery() -> None:
    captor = _CapturingAlerter()
    multi = MultiAlerter([captor])
    hb = HeartbeatMonitor(alerter=multi, default_timeout_s=30)
    hb.register("mnq")
    hb._last["mnq"] = datetime.now(UTC) - timedelta(seconds=120)
    await hb._alert_stale(hb.check_stale())
    await hb._alert_stale(hb.check_stale())
    assert len(captor.sent) == 1  # dedup via _alerted set


def test_deregister_removes_bot() -> None:
    hb = HeartbeatMonitor()
    hb.register("mnq")
    hb.deregister("mnq")
    assert hb.last_seen("mnq") is None
    assert hb.check_stale() == []
