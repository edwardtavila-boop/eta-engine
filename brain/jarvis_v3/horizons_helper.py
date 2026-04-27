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
from typing import Any

logger = logging.getLogger(__name__)


def projected_caps(ctx: Any, *, horizons: list[Any] | None = None) -> dict[Any, float]:
    """Return per-horizon size caps the bot should respect.

    When ``horizons.project`` is unavailable (module not present) OR
    raises, returns ``{}`` so callers can fall back to their static cap.
    Default horizons cover 1m / 5m / 15m / 1h / 4h.
    """
    try:
        from eta_engine.brain.jarvis_v3.horizons import Horizon, project

        if horizons is None:
            horizons = list(Horizon)

        out: dict[Any, float] = {}
        for h in horizons:
            try:
                projection = project(ctx, h)
                # The projected object is expected to expose a
                # `size_cap_mult` field per horizons.py's HorizonContext.
                cap = getattr(projection, "size_cap_mult", None)
                if cap is None:
                    # Fall back to inverse-stress heuristic if the field
                    # isn't on the projection object
                    stress = getattr(projection, "stress_composite", None)
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


def shortest_horizon_cap(ctx: Any) -> float:
    """Convenience: the cap for the shortest horizon, or 1.0 if unavailable.

    A bot scoping a tight scalp uses this. If horizons.project errors
    out, the bot operates as before (no cap from this layer).
    """
    caps = projected_caps(ctx)
    if not caps:
        return 1.0
    # Pick the smallest cap (most conservative across horizons)
    return min(caps.values())
