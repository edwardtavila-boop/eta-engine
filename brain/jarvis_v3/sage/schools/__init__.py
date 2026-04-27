"""ETA Engine // brain.jarvis_v3.sage.schools
=============================================
Implementations of each market-theory school.

Side-effect imports register every school in the sage registry.
"""
from __future__ import annotations

from eta_engine.brain.jarvis_v3.sage.schools import (  # noqa: F401
    dow_theory,
    elliott_wave,
    fibonacci,
    gann,
    market_profile,
    neowave,
    order_flow,
    risk_management,
    smc_ict,
    support_resistance,
    trend_following,
    vpa,
    weis_wyckoff,
    wyckoff,
)

__all__ = [
    "dow_theory",
    "elliott_wave",
    "fibonacci",
    "gann",
    "market_profile",
    "neowave",
    "order_flow",
    "risk_management",
    "smc_ict",
    "support_resistance",
    "trend_following",
    "vpa",
    "weis_wyckoff",
    "wyckoff",
]
