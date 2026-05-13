"""Multi-horizon decision helper (Tier-2 #9 wiring, 2026-04-27).

Wraps ``brain/jarvis_v3/horizons.py`` (which exposes ``Horizon``,
``HorizonStress``, ``HorizonContext``, ``project()``) into a
single-call helper for bots that want horizon-aware sizing.

Use case
--------
A bot that's about to enter on a 5-min signal can ask::

    from eta_engine.brain.jarvis_v3.horizons_helper import projected_caps

    caps = projected_caps(ctx, horizons=[Horizon.M5, Horizon.M15, Horizon.H1])
    # caps = {Horizon.M5: 0.50, Horizon.M15: 0.40, Horizon.H1: 0.30}

The bot picks the cap that matches its intended hold duration. Tighter
caps for shorter-horizon (less time for setups to play out) is the
typical pattern, but the projection function decides per regime.

This module is a THIN BOLT-ON. The actual projection math lives in
``horizons.py``; this just gives consumers a one-call API that's
backward-compatible (returns empty dict if horizons.project doesn't
exist or fails).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def projected_caps(ctx: object, *, horizons: list[object] | None = None) -> dict[object, float]:
    """Return per-horizon size caps the bot should respect.

    When ``horizons.project`` is unavailable (module not present) OR
    raises, returns ``{}`` so callers can fall back to their static cap.
    Default horizons cover 1m / 5m / 15m / 1h / 4h.
    """
    try:
        from eta_engine.brain.jarvis_v3.horizons import Horizon, project

        if horizons is None:
            horizons = list(Horizon)

        horizon_context = project(
            base_composite=_base_composite(ctx),
            base_binding=_base_binding(ctx),
            hours_until_event=_hours_until_event(ctx),
            event_label=_event_label(ctx),
            is_overnight_now=_is_overnight(ctx),
        )
        out: dict[object, float] = {}
        for h in horizons:
            try:
                projection = horizon_context.pick(h)
                # The projected object is expected to expose a
                # `size_cap_mult` field per horizons.py's HorizonContext.
                cap = getattr(projection, "size_cap_mult", None)
                if cap is None:
                    # Fall back to inverse-stress heuristic if the field
                    # isn't on the projection object
                    stress = getattr(
                        projection,
                        "stress_composite",
                        getattr(projection, "composite", None),
                    )
                    if isinstance(stress, (int, float)):
                        cap = max(0.1, 1.0 - float(stress))
                if isinstance(cap, (int, float)):
                    out[h] = round(float(cap), 4)
            except Exception as exc:  # noqa: BLE001
                logger.debug("horizon project failed for %s: %s", h, exc)
                continue
        return out
    except ImportError:
        logger.debug("horizons module unavailable; returning empty projections")
        return {}


def _base_composite(ctx: object) -> float:
    stress = getattr(ctx, "stress_score", None)
    raw = getattr(stress, "composite", getattr(ctx, "stress_composite", 0.0))
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def _base_binding(ctx: object) -> str:
    stress = getattr(ctx, "stress_score", None)
    return str(getattr(stress, "binding_constraint", getattr(ctx, "binding_constraint", "unknown")) or "unknown")


def _hours_until_event(ctx: object) -> float | None:
    macro = getattr(ctx, "macro", None)
    raw = getattr(macro, "hours_until_next_event", None)
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _event_label(ctx: object) -> str | None:
    macro = getattr(ctx, "macro", None)
    raw = getattr(macro, "next_event_label", None)
    return str(raw) if raw else None


def _is_overnight(ctx: object) -> bool:
    session = getattr(ctx, "session_phase", "")
    value = getattr(session, "value", session)
    return str(value).upper() == "OVERNIGHT"


def shortest_horizon_cap(ctx: object) -> float:
    """Convenience: the cap for the shortest horizon, or 1.0 if unavailable.

    A bot scoping a tight scalp uses this. If horizons.project errors
    out, the bot operates as before (no cap from this layer).
    """
    caps = projected_caps(ctx)
    if not caps:
        return 1.0
    # Pick the smallest cap (most conservative across horizons)
    return min(caps.values())
