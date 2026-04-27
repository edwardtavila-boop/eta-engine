"""
EVOLUTIONARY TRADING ALGO  //  brain.avengers.adaptive_cron
===============================================
Regime-aware gating layer on top of the daemon's static cron.

Why this exists
---------------
``TASK_CADENCE`` hard-codes one cron expression per BackgroundTask.
That's wrong in two directions:

  * In a flat / calm regime, ``DRIFT_SUMMARY`` every 15 minutes wastes
    Sonnet dollars on a stream that hasn't changed.
  * In a news-driven / stressed regime, the same 15-minute cadence is
    too slow -- the picture changes every tick.

This module lets the daemon ask "should this task fire RIGHT NOW, given
current regime?" before dispatching. The policy is simple:

  * If regime in {CALM, QUIET} and task is ``sparse_ok``: skip
    1-in-N fires to reduce frequency.
  * If regime in {STRESSED, NEWS}: always fire + optionally escalate
    some Sonnet tasks to run more often.

The daemon still owns the base schedule -- this is a boolean gate
layered on top, not a replacement for the cron table.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.avengers.dispatch import BackgroundTask

if TYPE_CHECKING:
    from collections.abc import Callable


class RegimeTag(StrEnum):
    """Coarse regime buckets the gate understands.

    Keep this small -- we just need "is today the kind of day that
    demands more frequent monitoring?". The canonical regime classifier
    in ``brain.regime`` is finer-grained; this enum is a projection of
    that onto {CALM, NORMAL, STRESSED, NEWS}.
    """

    CALM = "CALM"  # low vol, no event risk
    NORMAL = "NORMAL"  # default
    STRESSED = "STRESSED"  # elevated stress score
    NEWS = "NEWS"  # event window (FOMC, CPI, earnings peak)


# Which tasks are "sparse-ok" in calm regimes. These are diagnostics /
# retrospectives that can safely be skipped 1-in-N ticks when nothing
# interesting is happening.
_SPARSE_OK: frozenset[BackgroundTask] = frozenset(
    {
        BackgroundTask.DRIFT_SUMMARY,
        BackgroundTask.DASHBOARD_ASSEMBLE,
        BackgroundTask.LOG_COMPACT,
        BackgroundTask.SHADOW_TICK,
    }
)

# Which tasks should NEVER be skipped regardless of regime -- they
# either carry safety duties or are already rare.
_FIRE_ALWAYS: frozenset[BackgroundTask] = frozenset(
    {
        BackgroundTask.AUDIT_SUMMARIZE,
        BackgroundTask.KAIZEN_RETRO,
        BackgroundTask.DISTILL_TRAIN,
        BackgroundTask.CAUSAL_REVIEW,
        BackgroundTask.TWIN_VERDICT,
        BackgroundTask.DOCTRINE_REVIEW,
        BackgroundTask.STRATEGY_MINE,
        BackgroundTask.PROMPT_WARMUP,
    }
)


class GateDecision(BaseModel):
    """Why the gate said fire/skip. For journaling / CLI."""

    model_config = ConfigDict(frozen=True)

    task: str
    regime: str
    fire: bool
    reason: str = ""
    call_idx: int = Field(ge=0, default=0)


class RegimeGate:
    """Injectable gate the daemon consults before dispatching a task.

    Parameters
    ----------
    regime_getter
        Callable returning the current ``RegimeTag``. Tests pass a lambda;
        production wires to ``brain.regime.classify_regime``.
    calm_skip_ratio
        When regime is CALM, fire only 1-in-N ticks for sparse-ok tasks.
        3 means: fire the 1st, skip 2nd, skip 3rd, fire 4th, ...
    """

    def __init__(
        self,
        *,
        regime_getter: Callable[[], RegimeTag] | None = None,
        calm_skip_ratio: int = 3,
    ) -> None:
        self._regime_getter = regime_getter or (lambda: RegimeTag.NORMAL)
        self.calm_skip_ratio = max(1, int(calm_skip_ratio))
        self._counts: dict[BackgroundTask, int] = {}

    def should_fire(self, task: BackgroundTask) -> GateDecision:
        regime = self._regime_getter()
        idx = self._counts.get(task, 0)
        self._counts[task] = idx + 1

        if task in _FIRE_ALWAYS:
            return GateDecision(
                task=task.value,
                regime=regime.value,
                fire=True,
                reason="fire-always task",
                call_idx=idx,
            )

        if regime in {RegimeTag.STRESSED, RegimeTag.NEWS}:
            return GateDecision(
                task=task.value,
                regime=regime.value,
                fire=True,
                reason=f"fire on {regime.value}",
                call_idx=idx,
            )

        if regime is RegimeTag.CALM and task in _SPARSE_OK:
            # Skip everything except every Nth call.
            fire = (idx % self.calm_skip_ratio) == 0
            return GateDecision(
                task=task.value,
                regime=regime.value,
                fire=fire,
                reason=(f"calm regime, 1-in-{self.calm_skip_ratio} schedule"),
                call_idx=idx,
            )

        return GateDecision(
            task=task.value,
            regime=regime.value,
            fire=True,
            reason="normal regime",
            call_idx=idx,
        )

    def reset(self) -> None:
        """Wipe internal counters. For tests."""
        self._counts.clear()


__all__ = [
    "GateDecision",
    "RegimeGate",
    "RegimeTag",
]
