"""ETA Engine // brain.jarvis_v3.policies
========================================
Registry of named JARVIS policy candidates.

Importing this package auto-registers every shipped candidate via
``register_candidate(...)``. The bandit harness + the
``scripts/score_policy_candidate.py`` replay both pull candidates from
the registry by name.

Adding a new candidate
----------------------
1. Drop a new module here, e.g. ``v19_drift_aware.py``
2. Author ``def evaluate_v19(req, ctx) -> ActionResponse:`` matching the
   ``jarvis_admin.evaluate_request`` signature.
3. At module bottom, call::

       from eta_engine.brain.jarvis_v3.candidate_policy import register_candidate
       register_candidate("v19", evaluate_v19,
                          parent_version=18,
                          rationale="...")

4. Import the new module in this ``__init__.py`` so it auto-registers.

The current champion (``v17``) is the unmodified ``evaluate_request``
from ``jarvis_admin.py`` -- registered here as a passthrough so the
bandit always has a baseline arm.
"""
from __future__ import annotations

# Side-effect imports register the candidates.
from eta_engine.brain.jarvis_v3.policies import (  # noqa: F401
    v17_champion,
    v18_high_stress_tighten,
    v19_drift_aware,
    v20_overnight_tighten,
    v21_drawdown_proximity,
    v22_sage_confluence,
)

__all__ = [
    "v17_champion",
    "v18_high_stress_tighten",
    "v19_drift_aware",
    "v20_overnight_tighten",
    "v21_drawdown_proximity",
    "v22_sage_confluence",
]
