"""Tests for ``eta_engine.chaos.drills`` -- recipe registry + run_drill."""

from __future__ import annotations

import pytest
from eta_engine.chaos.drills import (
    DRILL_REGISTRY,
    DrillResult,
    DrillSpec,
    list_drills,
    run_drill,
)


def test_registry_has_built_in_drills() -> None:
    names = {d.name for d in list_drills()}
    assert {"chrony_kill", "redis_stall", "ws_disconnect_bybit",
            "dns_jam", "disk_pressure"}.issubset(names)


def test_run_drill_dry_run_is_safe() -> None:
    out = run_drill("chrony_kill", execute=False)
    assert isinstance(out, DrillResult)
    assert out.executed is False
    assert out.success is True
    assert out.observations.get("dry_run") is True


def test_run_drill_returns_observations_in_dry_run() -> None:
    out = run_drill("redis_stall", execute=False)
    # observe_fn for redis returns a 'ping' or 'error' field; in dry run we
    # additionally include dry_run=True.
    assert out.observations.get("dry_run") is True


def test_run_drill_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="unknown drill"):
        run_drill("does-not-exist")


def test_drill_spec_is_immutable() -> None:
    from dataclasses import FrozenInstanceError
    spec = DrillSpec(
        name="x", description="y", severity="low",
    )
    with pytest.raises(FrozenInstanceError):
        spec.name = "z"  # type: ignore[misc]


def test_run_drill_disk_pressure_does_not_apply_in_dry_run() -> None:
    # disk_pressure is severity=high; dry-run path should NOT touch disk.
    out = run_drill("disk_pressure", execute=False)
    assert out.executed is False
    assert "intent" in out.observations or "dry_run" in out.observations


def test_drill_severity_field() -> None:
    spec = DRILL_REGISTRY["chrony_kill"]
    assert spec.severity == "low"


def test_drill_blast_seconds_present() -> None:
    for spec in list_drills():
        assert spec.blast_seconds > 0


def test_run_drill_handles_apply_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inject a drill whose apply_fn raises.
    def boom() -> None:
        raise RuntimeError("kaboom")

    fake = DrillSpec(
        name="fake_boom",
        description="apply raises",
        severity="low",
        apply_fn=boom,
        recover_fn=lambda: None,
        observe_fn=lambda: {"observed": True},
        blast_seconds=0.01,
    )
    monkeypatch.setitem(DRILL_REGISTRY, "fake_boom", fake)
    out = run_drill("fake_boom", execute=True)
    assert out.executed is True
    assert out.success is False
    assert "apply exception" in out.notes


def test_run_drill_handles_recover_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_recover() -> None:
        raise RuntimeError("recover failed")

    fake = DrillSpec(
        name="fake_bad_recover",
        description="recover raises",
        severity="low",
        apply_fn=lambda: None,
        recover_fn=bad_recover,
        observe_fn=lambda: {},
        blast_seconds=0.01,
    )
    monkeypatch.setitem(DRILL_REGISTRY, "fake_bad_recover", fake)
    out = run_drill("fake_bad_recover", execute=True)
    assert out.success is False
    assert "recover exception" in out.notes
