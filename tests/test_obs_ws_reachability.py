"""Tests for ``eta_engine.obs.ws_reachability``."""

from __future__ import annotations

import pytest
from eta_engine.obs.ws_reachability import (
    EndpointHealth,
    EndpointStatus,
    FailoverEvent,
    PrimaryBackupRouter,
    ReachabilityMonitor,
)

# ---------------------------------------------------------------------------
# EndpointHealth -- status state machine
# ---------------------------------------------------------------------------


def test_unconnected_endpoint_is_unreachable() -> None:
    e = EndpointHealth(name="bybit-tokyo", url="wss://ex/1")
    assert e.status(now=100.0) == EndpointStatus.UNREACHABLE


def test_just_connected_with_recent_pong_is_healthy() -> None:
    e = EndpointHealth(name="bybit", url="wss://ex/1")
    e.observe_connected(now=0.0)
    e.observe_ping_sent(now=1.0)
    e.observe_pong(now=1.05)        # 50 ms RTT
    e.observe_frame(now=1.05)
    assert e.status(now=0.10) == EndpointStatus.HEALTHY


def test_high_rtt_marks_degraded() -> None:
    e = EndpointHealth(name="ex", url="x", rtt_warn_ms=100, rtt_unreachable_ms=2000)
    e.observe_connected(now=0.0)
    e.observe_ping_sent(now=1.0)
    e.observe_pong(now=1.30)        # 300 ms RTT > 100
    e.observe_frame(now=1.30)
    assert e.status(now=0.5) == EndpointStatus.DEGRADED


def test_extreme_rtt_marks_unreachable() -> None:
    e = EndpointHealth(name="ex", url="x", rtt_warn_ms=100, rtt_unreachable_ms=1000)
    e.observe_connected(now=0.0)
    e.observe_ping_sent(now=1.0)
    e.observe_pong(now=3.5)         # 2500 ms RTT
    e.observe_frame(now=3.5)
    assert e.status(now=3.0) == EndpointStatus.UNREACHABLE


def test_stale_frame_marks_stale() -> None:
    e = EndpointHealth(name="ex", url="x", stale_after_seconds=10)
    e.observe_connected(now=0.0)
    e.observe_ping_sent(now=1.0)
    e.observe_pong(now=1.05)
    e.observe_frame(now=0.0)
    # Long after last frame.
    assert e.status(now=20.0) == EndpointStatus.STALE


def test_no_recent_ping_marks_unreachable() -> None:
    e = EndpointHealth(
        name="ex", url="x", unreachable_after_seconds=30, stale_after_seconds=5,
    )
    e.observe_connected(now=0.0)
    e.observe_ping_sent(now=1.0)
    e.observe_pong(now=1.05)
    e.observe_frame(now=1.05)
    assert e.status(now=200.0) == EndpointStatus.UNREACHABLE


def test_disconnect_marks_unreachable() -> None:
    e = EndpointHealth(name="ex", url="x")
    e.observe_connected(now=0.0)
    e.observe_ping_sent(now=1.0)
    e.observe_pong(now=0.01)
    e.observe_frame(now=0.01)
    assert e.status(now=0.1) == EndpointStatus.HEALTHY
    e.observe_disconnected()
    assert e.status(now=0.2) == EndpointStatus.UNREACHABLE


def test_median_rtt_with_window() -> None:
    e = EndpointHealth(name="ex", url="x", rtt_window=4)
    for i, dt in enumerate([0.01, 0.02, 0.05, 0.10, 0.20, 0.30]):
        e.observe_ping_sent(now=float(i))
        e.observe_pong(now=float(i) + dt)
    # Window keeps last 4: [0.05, 0.10, 0.20, 0.30] -> median = (0.10+0.20)/2 = 0.15s
    assert e.median_rtt_ms() == pytest.approx(150.0)


def test_snapshot_shape() -> None:
    e = EndpointHealth(name="ex", url="x")
    e.observe_connected(now=0.0)
    e.observe_ping_sent(now=1.0)
    e.observe_pong(now=1.05)
    e.observe_frame(now=1.05)
    snap = e.snapshot(now=0.10)
    assert snap["name"] == "ex"
    assert snap["status"] == "HEALTHY"
    assert snap["connected"] is True
    assert isinstance(snap["rtt_ms_median"], float)


# ---------------------------------------------------------------------------
# ReachabilityMonitor -- selection
# ---------------------------------------------------------------------------


def _healthy(name: str, rtt_ms: float, frame_age: float = 0.0) -> EndpointHealth:
    e = EndpointHealth(name=name, url=f"wss://x/{name}")
    e.observe_connected(now=0.0)
    e.observe_ping_sent(now=1.0)
    e.observe_pong(now=1.0 + rtt_ms / 1000.0)
    e.observe_frame(now=10.0 - frame_age)
    return e


def test_monitor_picks_lowest_rtt_healthy() -> None:
    m = ReachabilityMonitor()
    m.register(_healthy("a", rtt_ms=50.0))
    m.register(_healthy("b", rtt_ms=20.0))
    m.register(_healthy("c", rtt_ms=80.0))
    pick = m.preferred(now=10.0)
    assert pick is not None
    assert pick.name == "b"


def test_monitor_returns_none_when_all_degraded() -> None:
    m = ReachabilityMonitor()
    bad = EndpointHealth(name="bad", url="x", rtt_warn_ms=10, rtt_unreachable_ms=20)
    bad.observe_connected(now=0.0)
    bad.observe_ping_sent(now=1.0)
    bad.observe_pong(now=1.5)        # 500 ms > 20 ms unreachable threshold
    bad.observe_frame(now=1.5)
    m.register(bad)
    assert m.preferred(now=1.0) is None


def test_monitor_degraded_lists_non_healthy() -> None:
    m = ReachabilityMonitor()
    m.register(_healthy("a", rtt_ms=10.0))
    bad = EndpointHealth(name="b", url="x", rtt_warn_ms=10, rtt_unreachable_ms=20)
    bad.observe_connected(now=0.0)
    bad.observe_ping_sent(now=1.0)
    bad.observe_pong(now=1.05)       # 50 ms > 10 ms warn threshold
    bad.observe_frame(now=1.05)
    m.register(bad)
    deg = m.degraded(now=1.0)
    assert {e.name for e in deg} == {"b"}


def test_monitor_snapshot_shape() -> None:
    m = ReachabilityMonitor()
    m.register(_healthy("a", rtt_ms=10.0))
    snap = m.snapshot(now=10.0)
    assert snap["preferred"] == "a"
    assert snap["all_healthy"] is True
    assert isinstance(snap["endpoints"], list)


def test_monitor_snapshot_empty_is_unhealthy() -> None:
    m = ReachabilityMonitor()
    snap = m.snapshot()
    assert snap["preferred"] is None
    assert snap["all_healthy"] is False


# ---------------------------------------------------------------------------
# PrimaryBackupRouter -- failover semantics
# ---------------------------------------------------------------------------


def test_router_starts_on_primary() -> None:
    primary = _healthy("p", rtt_ms=20.0)
    backup  = _healthy("b", rtt_ms=80.0)
    r = PrimaryBackupRouter(primary, backup)
    assert r.active().name == "p"
    assert r.poll(now=10.0) is None


def test_router_swaps_to_backup_on_primary_degraded() -> None:
    primary = _healthy("p", rtt_ms=20.0)
    backup  = _healthy("b", rtt_ms=80.0)
    r = PrimaryBackupRouter(primary, backup)
    primary.observe_disconnected()
    ev = r.poll(now=10.0)
    assert isinstance(ev, FailoverEvent)
    assert ev.from_name == "p" and ev.to_name == "b"
    assert r.active().name == "b"


def test_router_does_not_flap_back_until_grace_window() -> None:
    primary = _healthy("p", rtt_ms=20.0)
    backup  = _healthy("b", rtt_ms=80.0)
    r = PrimaryBackupRouter(primary, backup, recovery_grace_seconds=30.0)
    primary.observe_disconnected()
    r.poll(now=10.0)            # swap to backup
    primary.observe_connected(now=20.0)
    primary.observe_ping_sent(now=20.0)
    primary.observe_pong(now=20.05)
    primary.observe_frame(now=20.05)
    # Just recovered -- still on backup.
    assert r.poll(now=20.1) is None
    assert r.active().name == "b"
    # 10s into grace -- still on backup.
    primary.observe_ping_sent(now=30.0)
    primary.observe_pong(now=30.05)
    primary.observe_frame(now=30.05)
    assert r.poll(now=30.1) is None
    assert r.active().name == "b"
    # Past grace -- swap back.
    primary.observe_ping_sent(now=51.0)
    primary.observe_pong(now=51.05)
    primary.observe_frame(now=51.05)
    ev = r.poll(now=51.1)
    assert ev is not None
    assert ev.from_name == "b" and ev.to_name == "p"
    assert r.active().name == "p"


def test_router_resets_recovery_clock_when_primary_re_breaks() -> None:
    primary = _healthy("p", rtt_ms=20.0)
    backup  = _healthy("b", rtt_ms=80.0)
    r = PrimaryBackupRouter(primary, backup, recovery_grace_seconds=10.0)
    primary.observe_disconnected()
    r.poll(now=0.0)             # swap to backup

    # Primary recovers...
    primary.observe_connected(now=5.0)
    primary.observe_ping_sent(now=5.0)
    primary.observe_pong(now=5.05)
    primary.observe_frame(now=5.05)
    r.poll(now=5.1)             # grace clock starts

    # ...then re-breaks before grace expires.
    primary.observe_disconnected()
    r.poll(now=8.0)             # clock should reset

    # Primary re-recovers; grace-clock starts FRESH from the new
    # recovery moment.
    primary.observe_connected(now=20.0)
    primary.observe_ping_sent(now=20.0)
    primary.observe_pong(now=20.05)
    primary.observe_frame(now=20.05)
    r.poll(now=20.1)            # new clock starts

    # Just past *new* grace window from t=20:
    primary.observe_ping_sent(now=31.0)
    primary.observe_pong(now=31.05)
    primary.observe_frame(now=31.05)
    ev = r.poll(now=31.1)
    assert ev is not None
    assert ev.to_name == "p"
