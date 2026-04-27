"""Tests for obs.vps_redundancy -- failover controller + DNS switcher."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from pydantic import ValidationError

from eta_engine.obs.vps_redundancy import (
    FailoverController,
    FailoverPolicy,
    HealthSnapshot,
    StubDnsSwitchProvider,
    StubHealthProbe,
    VpsHealth,
    VpsRedundancyController,
    VpsRole,
)

_T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _clock_at(t: datetime) -> Callable[[], datetime]:
    def fn() -> datetime:
        return t

    return fn


def _snap(role: VpsRole, health: VpsHealth, *, ts: datetime = _T0) -> HealthSnapshot:
    return HealthSnapshot(ts=ts, role=role, health=health)


# --------------------------------------------------------------------------- #
# Enum completeness
# --------------------------------------------------------------------------- #


def test_vps_role_enum_members() -> None:
    assert {m.value for m in VpsRole} == {"PRIMARY", "SECONDARY"}


def test_vps_health_enum_members() -> None:
    assert {m.value for m in VpsHealth} == {"HEALTHY", "DEGRADED", "DOWN"}


# --------------------------------------------------------------------------- #
# FailoverPolicy validation
# --------------------------------------------------------------------------- #


def test_policy_defaults_are_sane() -> None:
    p = FailoverPolicy()
    assert p.primary_unhealthy_threshold == 3
    assert p.primary_recovery_threshold == 10
    assert p.secondary_unhealthy_threshold == 5
    assert p.degraded_counts_as_unhealthy is True


def test_policy_rejects_zero_thresholds() -> None:
    with pytest.raises(ValidationError):
        FailoverPolicy(primary_unhealthy_threshold=0)
    with pytest.raises(ValidationError):
        FailoverPolicy(primary_recovery_threshold=0)
    with pytest.raises(ValidationError):
        FailoverPolicy(secondary_unhealthy_threshold=0)


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stub_probe_returns_set_health() -> None:
    p = StubHealthProbe()
    p.set_health(VpsRole.PRIMARY, VpsHealth.DOWN)
    snap = await p.probe(VpsRole.PRIMARY)
    assert snap.role == VpsRole.PRIMARY
    assert snap.health == VpsHealth.DOWN
    assert VpsRole.PRIMARY in p.calls


@pytest.mark.asyncio
async def test_stub_probe_respects_latency_override() -> None:
    p = StubHealthProbe()
    p.set_latency(VpsRole.SECONDARY, 42.0)
    snap = await p.probe(VpsRole.SECONDARY)
    assert snap.latency_ms == 42.0


@pytest.mark.asyncio
async def test_stub_dns_switch_records_calls() -> None:
    d = StubDnsSwitchProvider()
    await d.switch(to_role=VpsRole.SECONDARY, reason="test")
    assert d.switches == [(VpsRole.SECONDARY, "test")]
    assert d.current == VpsRole.SECONDARY


@pytest.mark.asyncio
async def test_stub_dns_switch_fail_flag() -> None:
    d = StubDnsSwitchProvider(fail=True)
    with pytest.raises(RuntimeError, match="configured to fail"):
        await d.switch(to_role=VpsRole.SECONDARY, reason="test")


# --------------------------------------------------------------------------- #
# FailoverController: stable primary
# --------------------------------------------------------------------------- #


def test_controller_starts_as_primary() -> None:
    c = FailoverController()
    assert c.active_role == VpsRole.PRIMARY


def test_controller_does_not_flip_on_healthy() -> None:
    c = FailoverController(policy=FailoverPolicy(primary_unhealthy_threshold=2))
    for _ in range(5):
        c.observe(_snap(VpsRole.PRIMARY, VpsHealth.HEALTHY))
        c.observe(_snap(VpsRole.SECONDARY, VpsHealth.HEALTHY))
    assert c.decide(now=_T0) is None
    assert c.active_role == VpsRole.PRIMARY


# --------------------------------------------------------------------------- #
# FailoverController: flip to secondary
# --------------------------------------------------------------------------- #


def test_controller_flips_after_n_down_probes() -> None:
    c = FailoverController(policy=FailoverPolicy(primary_unhealthy_threshold=3))
    for _ in range(3):
        c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DOWN))
        c.observe(_snap(VpsRole.SECONDARY, VpsHealth.HEALTHY))
    evt = c.decide(now=_T0)
    assert evt is not None
    assert evt.from_role == VpsRole.PRIMARY
    assert evt.to_role == VpsRole.SECONDARY
    assert "unhealthy" in evt.reason
    assert c.active_role == VpsRole.SECONDARY


def test_controller_does_not_flip_below_threshold() -> None:
    c = FailoverController(policy=FailoverPolicy(primary_unhealthy_threshold=3))
    for _ in range(2):
        c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DOWN))
    evt = c.decide(now=_T0)
    assert evt is None
    assert c.active_role == VpsRole.PRIMARY


def test_controller_resets_after_one_healthy() -> None:
    """A single HEALTHY in the middle of a DOWN run clears the count."""
    c = FailoverController(policy=FailoverPolicy(primary_unhealthy_threshold=3))
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DOWN))
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DOWN))
    # Recovery breaks the streak
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.HEALTHY))
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DOWN))
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DOWN))
    evt = c.decide(now=_T0)
    assert evt is None  # only 2 consecutive unhealthy tail entries


def test_controller_degraded_counts_as_unhealthy_by_default() -> None:
    c = FailoverController(policy=FailoverPolicy(primary_unhealthy_threshold=3))
    for _ in range(3):
        c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DEGRADED))
    evt = c.decide(now=_T0)
    assert evt is not None
    assert evt.to_role == VpsRole.SECONDARY


def test_controller_degraded_ignored_when_disabled() -> None:
    c = FailoverController(
        policy=FailoverPolicy(
            primary_unhealthy_threshold=3,
            degraded_counts_as_unhealthy=False,
        ),
    )
    for _ in range(3):
        c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DEGRADED))
    evt = c.decide(now=_T0)
    assert evt is None
    assert c.active_role == VpsRole.PRIMARY


# --------------------------------------------------------------------------- #
# FailoverController: flip back to primary
# --------------------------------------------------------------------------- #


def test_controller_flips_back_to_primary_after_recovery() -> None:
    c = FailoverController(
        policy=FailoverPolicy(
            primary_unhealthy_threshold=2,
            primary_recovery_threshold=4,
        ),
        initial_role=VpsRole.SECONDARY,
    )
    for _ in range(4):
        c.observe(_snap(VpsRole.PRIMARY, VpsHealth.HEALTHY))
    evt = c.decide(now=_T0)
    assert evt is not None
    assert evt.from_role == VpsRole.SECONDARY
    assert evt.to_role == VpsRole.PRIMARY
    assert "healthy" in evt.reason
    assert c.active_role == VpsRole.PRIMARY


def test_controller_cautious_about_failback() -> None:
    """Default recovery threshold (10) > unhealthy threshold (3): it's
    much easier to fail over than to fail back, to avoid flap."""
    p = FailoverPolicy()
    assert p.primary_recovery_threshold > p.primary_unhealthy_threshold


def test_controller_does_not_flip_back_below_threshold() -> None:
    c = FailoverController(
        policy=FailoverPolicy(primary_recovery_threshold=5),
        initial_role=VpsRole.SECONDARY,
    )
    for _ in range(4):
        c.observe(_snap(VpsRole.PRIMARY, VpsHealth.HEALTHY))
    evt = c.decide(now=_T0)
    assert evt is None
    assert c.active_role == VpsRole.SECONDARY


def test_controller_failback_only_when_active_is_secondary() -> None:
    """should_flip_to_primary should not fire if we are already primary."""
    c = FailoverController(initial_role=VpsRole.PRIMARY)
    for _ in range(20):
        c.observe(_snap(VpsRole.PRIMARY, VpsHealth.HEALTHY))
    assert c.should_flip_to_primary() is False


# --------------------------------------------------------------------------- #
# FailoverController: secondary health
# --------------------------------------------------------------------------- #


def test_controller_detects_both_hosts_dying() -> None:
    c = FailoverController(
        policy=FailoverPolicy(secondary_unhealthy_threshold=3),
    )
    for _ in range(3):
        c.observe(_snap(VpsRole.SECONDARY, VpsHealth.DOWN))
    assert c.secondary_degraded() is True


def test_controller_secondary_degraded_false_when_healthy() -> None:
    c = FailoverController()
    for _ in range(20):
        c.observe(_snap(VpsRole.SECONDARY, VpsHealth.HEALTHY))
    assert c.secondary_degraded() is False


# --------------------------------------------------------------------------- #
# FailoverController: event log
# --------------------------------------------------------------------------- #


def test_controller_records_events() -> None:
    c = FailoverController(
        policy=FailoverPolicy(
            primary_unhealthy_threshold=2,
            primary_recovery_threshold=2,
        ),
    )
    # Fail over
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DOWN))
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.DOWN))
    c.decide(now=_T0)
    # Recover
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.HEALTHY))
    c.observe(_snap(VpsRole.PRIMARY, VpsHealth.HEALTHY))
    c.decide(now=_T0)
    assert len(c.events) == 2
    assert [e.to_role for e in c.events] == [VpsRole.SECONDARY, VpsRole.PRIMARY]


# --------------------------------------------------------------------------- #
# VpsRedundancyController end-to-end
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_redundancy_controller_tick_no_flip_on_healthy() -> None:
    probe = StubHealthProbe()
    dns = StubDnsSwitchProvider()
    ctrl = VpsRedundancyController(
        probe=probe,
        dns=dns,
        policy=FailoverPolicy(primary_unhealthy_threshold=2),
        clock=_clock_at(_T0),
    )
    evt = await ctrl.tick()
    assert evt is None
    assert dns.switches == []


@pytest.mark.asyncio
async def test_redundancy_controller_flips_and_calls_dns() -> None:
    probe = StubHealthProbe()
    probe.set_health(VpsRole.PRIMARY, VpsHealth.DOWN)
    dns = StubDnsSwitchProvider()
    ctrl = VpsRedundancyController(
        probe=probe,
        dns=dns,
        policy=FailoverPolicy(primary_unhealthy_threshold=2),
        clock=_clock_at(_T0),
    )
    # Two bad ticks -> flip
    evt1 = await ctrl.tick()
    assert evt1 is None
    evt2 = await ctrl.tick()
    assert evt2 is not None
    assert evt2.to_role == VpsRole.SECONDARY
    assert evt2.dns_updated is True
    assert dns.switches == [(VpsRole.SECONDARY, evt2.reason)]


@pytest.mark.asyncio
async def test_redundancy_controller_records_dns_failure() -> None:
    probe = StubHealthProbe()
    probe.set_health(VpsRole.PRIMARY, VpsHealth.DOWN)
    dns = StubDnsSwitchProvider(fail=True)
    ctrl = VpsRedundancyController(
        probe=probe,
        dns=dns,
        policy=FailoverPolicy(primary_unhealthy_threshold=1),
        clock=_clock_at(_T0),
    )
    evt = await ctrl.tick()
    assert evt is not None
    assert evt.dns_updated is False
    assert evt.dns_error is not None
    assert "configured to fail" in evt.dns_error


@pytest.mark.asyncio
async def test_redundancy_controller_treats_probe_exception_as_down() -> None:
    class BoomProbe:
        async def probe(self, role: VpsRole) -> HealthSnapshot:  # noqa: ARG002
            raise RuntimeError("network down")

    probe = BoomProbe()
    dns = StubDnsSwitchProvider()
    ctrl = VpsRedundancyController(
        probe=probe,
        dns=dns,
        policy=FailoverPolicy(primary_unhealthy_threshold=1),
        clock=_clock_at(_T0),
    )
    evt = await ctrl.tick()
    # One DOWN -> flip. Verify the flip event reason references unhealthy
    assert evt is not None
    assert evt.to_role == VpsRole.SECONDARY


@pytest.mark.asyncio
async def test_redundancy_controller_run_respects_max_ticks() -> None:
    probe = StubHealthProbe()
    dns = StubDnsSwitchProvider()
    ctrl = VpsRedundancyController(
        probe=probe,
        dns=dns,
        policy=FailoverPolicy(primary_unhealthy_threshold=2),
        clock=_clock_at(_T0),
    )
    await ctrl.run(interval_s=0.0, max_ticks=3)
    # Each tick probes both roles -> 6 probe calls total
    assert len(probe.calls) == 6


# --------------------------------------------------------------------------- #
# HealthSnapshot / FailoverEvent models
# --------------------------------------------------------------------------- #


def test_health_snapshot_default_ts_is_aware() -> None:
    s = HealthSnapshot(role=VpsRole.PRIMARY, health=VpsHealth.HEALTHY)
    assert s.ts.tzinfo is not None


def test_health_snapshot_fields() -> None:
    s = HealthSnapshot(
        ts=_T0,
        role=VpsRole.PRIMARY,
        health=VpsHealth.DEGRADED,
        latency_ms=120.5,
        detail="slow",
    )
    assert s.latency_ms == 120.5
    assert s.detail == "slow"
