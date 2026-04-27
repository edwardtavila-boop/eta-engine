"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.pnl_drift_drill.

Drill: inject a downward PnL regime change; verify the detector fires.

What this drill asserts
-----------------------
:class:`brain.pnl_drift.PageHinkleyDetector` is the online PageHinkley
CUSUM on per-trade R-PnL. The live orchestrator relies on two
guarantees:

* A long stationary stream of small positive R-PnL must NOT trip the
  alarm (false-positive rate stays low under routine wins).
* A regime break to sustained negative R-PnL must trip a "down"
  alarm within a bounded number of observations.

Silent regressions would either swamp the operator with alarms during
normal trading OR miss the regime change entirely until the bot has
already bled through a big sample.

The drill feeds:

1. 40 observations of ~+0.10R with small noise  -- expect no alarm.
2. Then 40 observations of ~-0.60R with the same noise -- expect a
   ``direction="down"`` alarm before observation 40.

Reset semantics are also verified: after an alarm the detector's
running state must re-initialize. The follow-on phase drives a second
regime-break (stationary +0.10R baseline -> +0.80R breakout) to prove
the reset was clean and the detector can fire an ``up`` alarm.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from eta_engine.brain.pnl_drift import PageHinkleyDetector
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_pnl_drift"]


def drill_pnl_drift(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Feed a stationary then regime-break PnL stream; verify alarm."""
    detector = PageHinkleyDetector(delta=0.005, threshold=1.0)
    rng = random.Random(4242)

    # Phase 1: 40 obs of +0.10R with small noise. No alarm allowed.
    for i in range(40):
        alarm = detector.update(0.10 + rng.gauss(0.0, 0.05))
        if alarm is not None:
            return drill_result(
                "pnl_drift",
                passed=False,
                details=f"stationary phase unexpectedly alarmed at obs {i}: {alarm.direction}",
            )

    # Phase 2: regime break, sustained negative PnL. Expect a "down" alarm.
    dn_alarm = None
    for i in range(40):
        got = detector.update(-0.60 + rng.gauss(0.0, 0.05))
        if got is not None:
            dn_alarm = got
            dn_obs_idx = i
            break
    if dn_alarm is None:
        return drill_result(
            "pnl_drift",
            passed=False,
            details="regime break did not trip a drift alarm within 40 obs",
        )
    if dn_alarm.direction != "down":
        return drill_result(
            "pnl_drift",
            passed=False,
            details=f"alarm direction was {dn_alarm.direction!r} (expected 'down')",
        )

    # Reset semantics: after the alarm fires, detector.reset() is called
    # internally, so its running state must be clean. Verify by driving a
    # fresh (stationary baseline -> regime break) cycle; an up alarm must
    # now fire with no residual accumulator from phase 2.
    if detector.n != 0 or detector.running_mean != 0.0:
        return drill_result(
            "pnl_drift",
            passed=False,
            details=(f"detector did not reset after alarm: n={detector.n} running_mean={detector.running_mean}"),
        )

    # Stationary baseline: 40 obs of ~+0.10R. No alarm allowed.
    for i in range(40):
        got = detector.update(0.10 + rng.gauss(0.0, 0.05))
        if got is not None:
            return drill_result(
                "pnl_drift",
                passed=False,
                details=(f"post-reset baseline phase unexpectedly alarmed at obs {i}: {got.direction}"),
            )

    # Regime break upward: 40 obs of ~+0.80R. Expect "up" alarm.
    up_alarm = None
    up_obs_idx = -1
    for i in range(40):
        got = detector.update(0.80 + rng.gauss(0.0, 0.05))
        if got is not None:
            up_alarm = got
            up_obs_idx = i
            break
    if up_alarm is None or up_alarm.direction != "up":
        return drill_result(
            "pnl_drift",
            passed=False,
            details=(f"post-reset up-drift did not fire an 'up' alarm within 40 obs (got {up_alarm!r})"),
            observed={
                "down_obs_idx": dn_obs_idx,
                "down_cumulative": dn_alarm.cumulative,
            },
        )

    return drill_result(
        "pnl_drift",
        passed=True,
        details="down alarm fired; detector reset cleanly; up alarm also fired",
        observed={
            "down_obs_idx": dn_obs_idx,
            "down_threshold": dn_alarm.threshold,
            "up_obs_idx": up_obs_idx,
            "up_direction": up_alarm.direction,
        },
    )
