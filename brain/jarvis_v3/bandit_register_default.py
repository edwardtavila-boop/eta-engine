"""Wire the default ETA bandit harness with the shipped policies (2026-04-27).

Imports the policies package (auto-registers all candidates), then
plugs them into the singleton ``BanditHarness``:

  * v17 = champion (passthrough to evaluate_request)
  * v18 = high-stress CONDITIONAL cap tightening

Importing this module is enough to wire the harness. Live consumers do::

    from eta_engine.brain.jarvis_v3.bandit_register_default import bandit_with_etas
    h = bandit_with_etas()
    arm = h.choose_arm()           # champion when ETA_BANDIT_ENABLED=false
    resp = arm.policy(req, ctx)
    ...
    h.observe_outcome(arm.arm_id, reward=R)

While ``ETA_BANDIT_ENABLED=false`` (default), ``choose_arm()`` always
returns v17 -- v18's ``observe_outcome`` calls do nothing because the
arm is never picked. Flipping ``ETA_BANDIT_ENABLED=true`` activates the
epsilon-greedy split (10% v18, 90% v17 in the default config).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def bandit_with_etas():
    """Get the default singleton harness, lazy-registered with v17+v18.

    Idempotent: re-calls return the same singleton without re-registering.
    """
    # Side-effect import: auto-registers v17 + v18 in the candidate registry.
    from eta_engine.brain.jarvis_v3 import policies  # noqa: F401
    from eta_engine.brain.jarvis_v3.bandit_harness import default_harness
    from eta_engine.brain.jarvis_v3.candidate_policy import get_candidate

    h = default_harness()
    if "v17" not in h.arms:
        try:
            h.register_arm("v17", get_candidate("v17"), is_champion=True)
            logger.info("bandit: registered champion v17")
        except (KeyError, ValueError) as exc:
            logger.warning("could not register v17: %s", exc)
    for arm in ("v18", "v19", "v20", "v21", "v22"):
        if arm in h.arms:
            continue
        try:
            h.register_arm(arm, get_candidate(arm))
            logger.info("bandit: registered candidate %s", arm)
        except (KeyError, ValueError) as exc:
            logger.warning("could not register %s: %s", arm, exc)
    return h
