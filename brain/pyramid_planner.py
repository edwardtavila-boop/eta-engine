"""Pyramiding / add-on framework (Tier-2 #10, 2026-04-27).

Shared abstraction for "scale into a winner" decisions. Bots
historically handled this ad-hoc; this module gives a consistent
surface so JARVIS can audit + the bandit can A/B different schemes.

Rules
-----
A pyramid plan defines:
  * max_adds                 -- ceiling on total scale-ins
  * min_progress_r           -- minimum favorable R-distance from
                                last entry before adding
  * min_minutes_between      -- time-spacing
  * add_size_pct             -- size relative to original entry
                                (typically 0.5x or smaller)
  * tighten_stops            -- whether to ratchet the stop on add

A pyramid request flows through ``can_add_now()`` -- which is a
pure function that returns ``AddDecision(allowed, reason)`` so the
bot or JARVIS can audit each decision without side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class PyramidPlan:
    """Configuration for a bot's pyramiding behavior."""

    max_adds: int = 2
    min_progress_r: float = 1.0
    min_minutes_between: int = 20
    add_size_pct: float = 0.5
    tighten_stops: bool = True


@dataclass
class PyramidState:
    """Mutable per-position pyramid state."""

    adds_so_far: int = 0
    last_add_ts: datetime | None = None
    last_add_price: float = 0.0
    initial_entry_price: float = 0.0
    initial_stop_distance_r: float = 1.0


@dataclass(frozen=True)
class AddDecision:
    allowed: bool
    reason_code: str
    reason: str


def can_add_now(
    *,
    plan: PyramidPlan,
    state: PyramidState,
    current_price: float,
    direction: str,
    now: datetime | None = None,
) -> AddDecision:
    """Pure function: should this position scale in NOW?

    Returns ``AddDecision`` with allowed=False + reason_code on rejection.
    """
    now = now or datetime.now(UTC)

    # Cap on adds
    if state.adds_so_far >= plan.max_adds:
        return AddDecision(
            allowed=False,
            reason_code="max_adds_reached",
            reason=f"already added {state.adds_so_far} times (max {plan.max_adds})",
        )

    # Time spacing
    if state.last_add_ts is not None:
        delta_min = (now - state.last_add_ts).total_seconds() / 60
        if delta_min < plan.min_minutes_between:
            return AddDecision(
                allowed=False,
                reason_code="too_soon",
                reason=f"{delta_min:.1f}m since last add (min {plan.min_minutes_between}m)",
            )

    # R-progress check: current price must be at least min_progress_r
    # FAVORABLE from last add
    if state.last_add_price > 0 and state.initial_stop_distance_r > 0:
        progress = current_price - state.last_add_price
        if direction.lower() in ("short", "sell"):
            progress = -progress
        progress_r = progress / state.initial_stop_distance_r
        if progress_r < plan.min_progress_r:
            return AddDecision(
                allowed=False,
                reason_code="insufficient_r_progress",
                reason=f"only {progress_r:.2f}R favorable since last add (need {plan.min_progress_r}R)",
            )

    return AddDecision(
        allowed=True,
        reason_code="approved",
        reason=f"pyramid add #{state.adds_so_far + 1} approved",
    )


def commit_add(
    state: PyramidState,
    *,
    at_price: float,
    when: datetime | None = None,
) -> PyramidState:
    """Update state after a confirmed pyramid add."""
    now = when or datetime.now(UTC)
    return PyramidState(
        adds_so_far=state.adds_so_far + 1,
        last_add_ts=now,
        last_add_price=at_price,
        initial_entry_price=state.initial_entry_price,
        initial_stop_distance_r=state.initial_stop_distance_r,
    )
