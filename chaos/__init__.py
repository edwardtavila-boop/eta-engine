"""
EVOLUTIONARY TRADING ALGO  //  chaos
====================================
Scheduled chaos drills for the live VPS.

Failure paths only stay alive if they're exercised. This subpackage
defines a small set of recipe + scheduler primitives the avengers
daemon can dispatch monthly:

* :mod:`chaos.drills`     -- recipes (kill chrony, jam DNS, drop WS).
* :mod:`chaos.scheduler`  -- rolling cadence + lockout for active sessions.

All recipes are *intent-only* by default: they emit alert events
describing what they would do. Set ``execute=True`` on the runner to
actually apply the failure (used in staging only).
"""

from eta_engine.chaos.drills import (
    DRILL_REGISTRY,
    DrillResult,
    DrillSpec,
    list_drills,
    run_drill,
)
from eta_engine.chaos.scheduler import (
    ChaosScheduleEntry,
    ChaosScheduler,
)

__all__ = [
    "ChaosScheduleEntry",
    "ChaosScheduler",
    "DRILL_REGISTRY",
    "DrillResult",
    "DrillSpec",
    "list_drills",
    "run_drill",
]
