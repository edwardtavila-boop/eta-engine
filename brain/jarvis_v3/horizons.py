"""
JARVIS v3 // horizons
=====================
Multi-horizon context.

v2 stress is a single scalar tied to "now". But an FOMC print 30 minutes away
should drive the NEXT_30M stress higher than the NOW stress. Split into four
horizons and let callers pick the one matching the action's time scope.

Pure / deterministic.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Horizon(StrEnum):
    NOW = "NOW"
    NEXT_15M = "NEXT_15M"
    NEXT_1H = "NEXT_1H"
    OVERNIGHT = "OVERNIGHT"


# How much a pending macro event at exactly T hours away contributes to each
# horizon's macro_event stress. Hand-tuned:
#   * NOW       -- only fires when event is <15min away
#   * NEXT_15M  -- fires hard 0..15min, softly 15..30min
#   * NEXT_1H   -- fires 0..1h
#   * OVERNIGHT -- fires if any event is within the next 16h
_HORIZON_HOURS: dict[Horizon, float] = {
    Horizon.NOW: 0.25,
    Horizon.NEXT_15M: 0.50,
    Horizon.NEXT_1H: 1.50,
    Horizon.OVERNIGHT: 16.0,
}


class HorizonStress(BaseModel):
    """Stress projected onto a specific horizon."""

    model_config = ConfigDict(frozen=True)

    horizon: Horizon
    composite: float = Field(ge=0.0, le=1.0)
    binding_constraint: str = Field(min_length=1)
    reasons: list[str] = Field(default_factory=list)


class HorizonContext(BaseModel):
    """A set of stress projections keyed by horizon."""

    model_config = ConfigDict(frozen=True)

    now: HorizonStress
    next_15m: HorizonStress
    next_1h: HorizonStress
    overnight: HorizonStress

    def pick(self, h: Horizon) -> HorizonStress:
        return {
            Horizon.NOW: self.now,
            Horizon.NEXT_15M: self.next_15m,
            Horizon.NEXT_1H: self.next_1h,
            Horizon.OVERNIGHT: self.overnight,
        }[h]

    @property
    def max_composite(self) -> float:
        return max(
            h.composite
            for h in (
                self.now,
                self.next_15m,
                self.next_1h,
                self.overnight,
            )
        )

    @property
    def binding_horizon(self) -> Horizon:
        """Which horizon has the highest composite -- the one to worry about."""
        ranked = sorted(
            (
                (h.horizon, h.composite)
                for h in (
                    self.now,
                    self.next_15m,
                    self.next_1h,
                    self.overnight,
                )
            ),
            key=lambda kv: kv[1],
            reverse=True,
        )
        return ranked[0][0]


def project(
    *,
    base_composite: float,
    base_binding: str,
    hours_until_event: float | None,
    event_label: str | None = None,
    is_overnight_now: bool = False,
) -> HorizonContext:
    """Derive per-horizon stress from a single base composite.

    Rules:
      1. NOW always equals base_composite (that's v2 behaviour).
      2. If a macro event is pending at T hours, the horizon whose scope
         straddles T gets bumped: bump = clip(1 - (T/scope_h), 0, 1) * 0.6
         so a 30min-away event bumps NEXT_15M by ~0.4, NEXT_1H by ~0.3.
      3. Overnight is the max of (base, 0.4 if is_overnight_now else 0.0)
         because overnight sessions have thinner liquidity even when quiet.
    """
    base = max(0.0, min(1.0, float(base_composite)))
    horizons: dict[Horizon, HorizonStress] = {}

    for h, scope_h in _HORIZON_HOURS.items():
        composite = base
        reasons = [f"base={base:.2f}"]
        binding = base_binding
        if hours_until_event is not None and 0 <= hours_until_event <= scope_h:
            bump = max(0.0, 1.0 - hours_until_event / scope_h) * 0.6
            composite = min(1.0, composite + bump)
            if event_label:
                reasons.append(
                    f"event '{event_label}' in {hours_until_event:.2f}h -> +{bump:.2f}",
                )
            binding = "macro_event"
        if h == Horizon.OVERNIGHT and is_overnight_now:
            floor = max(composite, 0.40)
            if floor > composite:
                reasons.append(f"overnight floor 0.40 -> {floor:.2f}")
                composite = floor
                binding = "session_overnight"
        horizons[h] = HorizonStress(
            horizon=h,
            composite=round(composite, 4),
            binding_constraint=binding,
            reasons=reasons,
        )

    return HorizonContext(
        now=horizons[Horizon.NOW],
        next_15m=horizons[Horizon.NEXT_15M],
        next_1h=horizons[Horizon.NEXT_1H],
        overnight=horizons[Horizon.OVERNIGHT],
    )
