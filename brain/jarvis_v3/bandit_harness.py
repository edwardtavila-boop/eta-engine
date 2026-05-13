"""Bandit allocation harness for policy A/B testing (Tier-2 #8, 2026-04-27).

Hooks ``brain/jarvis_v3/bandit.py``-style allocation into the verdict
pipeline so that a gated fraction of decisions can flow through a
CANDIDATE policy alongside the CHAMPION.

Workflow once activated::

  1. Operator authors a candidate policy (Python callable matching
     evaluate_request signature) and registers it via
     ``BanditHarness.register_arm("v18", evaluate_v18)``
  2. Each incoming ActionRequest is routed to one arm via Thompson
     sampling (or epsilon-greedy)
  3. Outcome (realized P&L from the resulting trade) is fed back via
     ``observe_outcome(arm_id, reward)``
  4. Bandit converges to the better arm; the harness reports per-arm
     reward distributions for the operator to inspect
  5. When confidence on the candidate exceeds a threshold, operator
     promotes it via the Tier-1 promotion gate (#4)

The multi-arm dispatch and reward-feedback plumbing is implemented,
but live routing is fail-closed behind ``ETA_BANDIT_ENABLED=true`` and
the operator's explicit go-ahead.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

# Feature flag: bandit is OFF by default so existing behavior is preserved.
BANDIT_ENABLED: bool = os.environ.get("ETA_BANDIT_ENABLED", "false").lower() in ("1", "true", "yes")


class _PolicyCallable(Protocol):
    """Signature matching jarvis_admin.evaluate_request -- but without
    importing jarvis_admin to avoid a circular reference at import time."""

    def __call__(self, req: object, ctx: object) -> object: ...


@dataclass
class _Arm:
    arm_id: str
    policy: _PolicyCallable
    pulls: int = 0
    rewards: list[float] = field(default_factory=list)

    @property
    def mean_reward(self) -> float:
        return sum(self.rewards) / len(self.rewards) if self.rewards else 0.0


class BanditHarness:
    """Multi-armed bandit harness for policy A/B testing.

    Default strategy is epsilon-greedy with ``epsilon=0.1`` -- 10% of
    decisions explore (random arm), 90% exploit (best mean-reward).

    Replace with Thompson sampling (Beta-Bernoulli) once the reward
    signal is real-money P&L rather than synthetic.
    """

    def __init__(self, *, epsilon: float = 0.10) -> None:
        self.arms: dict[str, _Arm] = {}
        self.epsilon = epsilon
        self.champion_id: str | None = None  # always the safe default

    def register_arm(self, arm_id: str, policy: _PolicyCallable, *, is_champion: bool = False) -> None:
        if arm_id in self.arms:
            raise ValueError(f"arm '{arm_id}' already registered")
        self.arms[arm_id] = _Arm(arm_id=arm_id, policy=policy)
        if is_champion:
            self.champion_id = arm_id
        logger.info("bandit registered arm=%s champion=%s", arm_id, is_champion)

    def choose_arm(self) -> _Arm:
        """Pick an arm to handle the next request.

        When ``BANDIT_ENABLED`` is False, always returns the champion
        (no exploration). Otherwise: epsilon-greedy.
        """
        if not BANDIT_ENABLED or self.champion_id is None:
            if self.champion_id is None:
                raise RuntimeError("bandit has no champion registered")
            return self.arms[self.champion_id]
        if random.random() < self.epsilon:
            return random.choice(list(self.arms.values()))
        # exploit: highest mean reward (champion if all tied)
        return max(self.arms.values(), key=lambda a: (a.mean_reward, a.arm_id == self.champion_id))

    def observe_outcome(self, arm_id: str, reward: float) -> None:
        """Feed back the realized reward (typically R-multiple of the
        resulting trade) to the arm that decided it."""
        if arm_id not in self.arms:
            logger.warning("ignoring outcome for unknown arm '%s'", arm_id)
            return
        a = self.arms[arm_id]
        a.pulls += 1
        a.rewards.append(reward)

    def report(self) -> dict[str, dict[str, float]]:
        return {
            arm_id: {
                "pulls": a.pulls,
                "mean_reward": round(a.mean_reward, 4),
                "is_champion": a.arm_id == self.champion_id,
            }
            for arm_id, a in self.arms.items()
        }


# Singleton (lazy)
_default_harness: BanditHarness | None = None


def default_harness() -> BanditHarness:
    global _default_harness
    if _default_harness is None:
        _default_harness = BanditHarness()
    return _default_harness
