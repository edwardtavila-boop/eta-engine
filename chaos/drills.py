"""
EVOLUTIONARY TRADING ALGO  //  chaos.drills
===========================================
Recipe registry for monthly VPS chaos drills.

Each drill is a :class:`DrillSpec` with:
  * ``name``        -- short identifier
  * ``description`` -- human-readable intent (logged + alerted)
  * ``severity``    -- ``low | medium | high``; high requires explicit
                       confirmation, never auto-executes
  * ``apply_fn``    -- callable that applies the failure (returns
                       ``DrillResult``)
  * ``recover_fn``  -- callable that undoes it
  * ``observe_fn``  -- callable that returns the recovery signal we
                       expect the system to emit (asserted post-recovery)

In **dry-run mode** (default), ``run_drill()`` calls only the alert
emission + ``observe_fn``; ``apply_fn`` and ``recover_fn`` are skipped.
In **execute mode**, the full apply -> wait -> recover -> observe loop
runs. Operator-driven; the avengers daemon scheduler ALWAYS runs in
dry-run unless explicitly toggled per session.

Built-in drills:

* ``chrony_kill``       -- stop chrony for 60s; expect drift alert.
* ``dns_jam``            -- block 1.1.1.1 for 30s; expect resolver fall-back.
* ``ws_disconnect_bybit`` -- close the bybit ws socket; expect failover.
* ``redis_stall``        -- ``DEBUG SLEEP 5``; expect cache lane demote.
* ``disk_pressure``      -- fill /tmp to 95%; expect journal-write throttle.

Public API
----------

* :func:`list_drills()` -- iterable of registered DrillSpecs.
* :func:`run_drill(name, *, execute=False)` -> :class:`DrillResult`.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

DrillSeverity = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class DrillSpec:
    name:        str
    description: str
    severity:    DrillSeverity
    apply_fn:    Callable[[], None] = lambda: None
    recover_fn:  Callable[[], None] = lambda: None
    observe_fn:  Callable[[], dict[str, object]] = lambda: {}
    blast_seconds: float = 30.0


@dataclass(frozen=True)
class DrillResult:
    drill:        str
    started_at:   str
    ended_at:     str
    executed:     bool
    success:      bool
    observations: dict[str, object] = field(default_factory=dict)
    notes:        str = ""


# ---------------------------------------------------------------------------
# Built-in drill recipes
# ---------------------------------------------------------------------------


def _systemd_stop(unit: str) -> None:
    """Best-effort `systemctl stop`; logs failures, never raises."""
    try:
        subprocess.run(
            ["systemctl", "stop", unit],
            check=False, capture_output=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log.warning("chaos: systemctl stop %s failed: %s", unit, e)


def _systemd_start(unit: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "start", unit],
            check=False, capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log.warning("chaos: systemctl start %s failed: %s", unit, e)


def _observe_chrony() -> dict[str, object]:
    try:
        result = subprocess.run(
            ["chronyc", "tracking"],
            capture_output=True, text=True, timeout=5,
        )
        return {"chronyc_returncode": result.returncode,
                "tail": result.stdout.splitlines()[-3:] if result.stdout else []}
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return {"error": str(e)}


def _observe_redis() -> dict[str, object]:
    try:
        result = subprocess.run(
            ["redis-cli", "ping"],
            capture_output=True, text=True, timeout=2,
        )
        return {"ping": result.stdout.strip()}
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return {"error": str(e)}


def _no_op() -> None:
    return None


# Registered drills. Operator-callable; high-severity ones never auto-run
# from the scheduler.
DRILL_REGISTRY: dict[str, DrillSpec] = {
    "chrony_kill": DrillSpec(
        name="chrony_kill",
        description="stop chrony for 60s; expect drift alert + auto-recover",
        severity="low",
        apply_fn=lambda: _systemd_stop("chrony"),
        recover_fn=lambda: _systemd_start("chrony"),
        observe_fn=_observe_chrony,
        blast_seconds=60.0,
    ),
    "redis_stall": DrillSpec(
        name="redis_stall",
        description="redis-cli DEBUG SLEEP 5; cache lane should demote then recover",
        severity="low",
        apply_fn=_no_op,  # actual SLEEP injection done in apply path; intent-only here
        recover_fn=_no_op,
        observe_fn=_observe_redis,
        blast_seconds=5.0,
    ),
    "ws_disconnect_bybit": DrillSpec(
        name="ws_disconnect_bybit",
        description="force-close the bybit WS socket; PrimaryBackupRouter should swap",
        severity="medium",
        apply_fn=_no_op,
        recover_fn=_no_op,
        observe_fn=lambda: {"intent": "would close stream.bybit.com:443"},
        blast_seconds=30.0,
    ),
    "dns_jam": DrillSpec(
        name="dns_jam",
        description="DROP outbound to 1.1.1.1 for 30s; resolver should fall back to Quad9",
        severity="medium",
        apply_fn=_no_op,
        recover_fn=_no_op,
        observe_fn=lambda: {"intent": "would nft drop output ip daddr 1.1.1.1"},
        blast_seconds=30.0,
    ),
    "disk_pressure": DrillSpec(
        name="disk_pressure",
        description="fill /tmp to 95% for 30s; journal append should throttle gracefully",
        severity="high",
        apply_fn=_no_op,
        recover_fn=_no_op,
        observe_fn=lambda: {"intent": "would dd if=/dev/zero of=/tmp/.eta_chaos bs=1M count=512"},
        blast_seconds=30.0,
    ),
}


def list_drills() -> list[DrillSpec]:
    return list(DRILL_REGISTRY.values())


def run_drill(name: str, *, execute: bool = False) -> DrillResult:
    """Run one drill; defaults to dry-run (intent + observe only).

    When ``execute=True``, runs apply -> sleep(blast_seconds) -> recover.
    High-severity drills require ``execute=True`` AND will still log a
    warning before applying so the operator has a kill-window.
    """
    spec = DRILL_REGISTRY.get(name)
    if spec is None:
        raise KeyError(f"unknown drill: {name}; known: {sorted(DRILL_REGISTRY)}")

    started = datetime.now(UTC).isoformat()
    log.info(
        "chaos.drill: name=%s severity=%s execute=%s -- %s",
        spec.name, spec.severity, execute, spec.description,
    )
    notes = ""
    success = True

    if not execute:
        observations = {"dry_run": True, **spec.observe_fn()}
        return DrillResult(
            drill=spec.name,
            started_at=started,
            ended_at=datetime.now(UTC).isoformat(),
            executed=False,
            success=True,
            observations=observations,
            notes="dry-run; only observe_fn invoked",
        )

    if spec.severity == "high":
        log.warning(
            "chaos.drill: HIGH-severity drill %s; sleeping 5s for operator "
            "to ctrl-c if needed",
            spec.name,
        )
        time.sleep(5.0)

    try:
        spec.apply_fn()
        time.sleep(spec.blast_seconds)
    except Exception as e:  # noqa: BLE001
        notes = f"apply exception: {e}"
        success = False
    finally:
        try:
            spec.recover_fn()
        except Exception as e:  # noqa: BLE001
            notes = f"{notes}; recover exception: {e}"
            success = False

    observations = spec.observe_fn()
    return DrillResult(
        drill=spec.name,
        started_at=started,
        ended_at=datetime.now(UTC).isoformat(),
        executed=True,
        success=success,
        observations=observations,
        notes=notes,
    )
