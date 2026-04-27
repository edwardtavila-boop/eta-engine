"""
JARVIS v3 // bandit
===================
Contextual LLM-routing bandit.

``model_policy.select_model`` is a static lookup: category -> tier. This
works, but it ignores evidence. Some CODE_REVIEW tasks genuinely need
Opus (a subtle concurrency bug), while others are trivial and Haiku would
suffice. We want the system to LEARN.

Approach: one-armed Beta-Bernoulli Thompson sampling per (category, tier)
arm. Reward is 1 if the invocation "succeeded" (caller-defined: test
pass, no reversion, operator approval, etc.), 0 otherwise. Cost-adjusted
reward = reward / cost_ratio so a successful HAIKU wins more than a
successful OPUS.

The bandit never downgrades OPUS for architectural categories where the
operator mandate forbids demotion -- those stay pinned via
``PINNED_CATEGORIES`` exactly as model_policy has them.

Pure stdlib + pydantic. Persistence via ``load()`` / ``save()`` to JSON.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.model_policy import (
    _CATEGORY_TO_TIER,
    COST_RATIO,
    ModelTier,
    TaskCategory,
)

# Categories we refuse to demote, no matter what the bandit learns.
# Mirror of the Opus-pinned bucket in model_policy.
PINNED_CATEGORIES: frozenset[TaskCategory] = frozenset(
    {
        TaskCategory.RED_TEAM_SCORING,
        TaskCategory.GAUNTLET_GATE_DESIGN,
        TaskCategory.RISK_POLICY_DESIGN,
        TaskCategory.ARCHITECTURE_DECISION,
        TaskCategory.ADVERSARIAL_REVIEW,
        TaskCategory.STATE_MACHINE_DESIGN,
    }
)


class ArmStats(BaseModel):
    """Beta-Bernoulli arm: alpha (successes+1), beta (failures+1)."""

    model_config = ConfigDict(frozen=False)

    alpha: float = Field(default=1.0, ge=0.0)
    beta: float = Field(default=1.0, ge=0.0)
    pulls: int = Field(default=0, ge=0)

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def sample(self, rng: random.Random) -> float:
        # Beta sample via two Gammas with shape 1 (exponentials).
        # Use Python's stdlib random.betavariate to keep it simple.
        return rng.betavariate(self.alpha, self.beta)

    def update(self, reward: int) -> None:
        if reward:
            self.alpha += 1.0
        else:
            self.beta += 1.0
        self.pulls += 1


class BanditState(BaseModel):
    """Persisted state: one ArmStats per (category, tier) pair."""

    model_config = ConfigDict(frozen=False)

    arms: dict[str, ArmStats] = Field(default_factory=dict)

    @staticmethod
    def _key(cat: TaskCategory, tier: ModelTier) -> str:
        return f"{cat.value}::{tier.value}"

    def get(self, cat: TaskCategory, tier: ModelTier) -> ArmStats:
        k = self._key(cat, tier)
        if k not in self.arms:
            self.arms[k] = ArmStats()
        return self.arms[k]

    def update(self, cat: TaskCategory, tier: ModelTier, reward: int) -> None:
        self.get(cat, tier).update(reward)


class BanditSelection(BaseModel):
    """Result of a ``select_tier`` call."""

    model_config = ConfigDict(frozen=True)

    category: TaskCategory
    tier: ModelTier
    reason: str = Field(min_length=1)
    exploratory: bool = False
    sampled_p: float = Field(ge=0.0, le=1.0)


class LLMBandit:
    """Thompson-sampling bandit over ``ModelTier`` per ``TaskCategory``.

    Parameters
    ----------
    state : BanditState
    rng   : injected random.Random (so tests can seed)
    min_pulls_before_bandit : until each tier has this many pulls, fall
        back to the static policy (cold-start protection).
    cost_weight : higher -> bandit prefers cheaper tiers at tie-break
        time. 0.0 disables cost adjustment.
    """

    def __init__(
        self,
        state: BanditState | None = None,
        rng: random.Random | None = None,
        *,
        min_pulls_before_bandit: int = 10,
        cost_weight: float = 0.3,
    ) -> None:
        self.state = state or BanditState()
        self.rng = rng or random.Random()
        self.min_pulls_before_bandit = min_pulls_before_bandit
        self.cost_weight = cost_weight

    def select_tier(self, category: TaskCategory) -> BanditSelection:
        """Pick a tier. Honors PINNED_CATEGORIES. Falls back to policy
        until the cold-start threshold is met.
        """
        if category in PINNED_CATEGORIES:
            tier = _CATEGORY_TO_TIER[category]
            return BanditSelection(
                category=category,
                tier=tier,
                reason=f"{category.value} pinned -> {tier.value}",
                exploratory=False,
                sampled_p=1.0,
            )

        tiers = list(ModelTier)
        pull_counts = {t: self.state.get(category, t).pulls for t in tiers}
        if min(pull_counts.values()) < self.min_pulls_before_bandit:
            tier = _CATEGORY_TO_TIER.get(category, ModelTier.SONNET)
            return BanditSelection(
                category=category,
                tier=tier,
                reason="cold-start: policy default",
                exploratory=True,
                sampled_p=0.0,
            )

        # Thompson sample per tier, adjust by cost.
        samples: dict[ModelTier, float] = {}
        for t in tiers:
            p = self.state.get(category, t).sample(self.rng)
            # Cost-adjust: divide by cost ratio ^ cost_weight
            cost = COST_RATIO[t]
            adjusted = p / (cost**self.cost_weight) if cost > 0 else p
            samples[t] = adjusted
        best_tier, best_p = max(samples.items(), key=lambda kv: kv[1])
        return BanditSelection(
            category=category,
            tier=best_tier,
            reason=f"thompson: adjusted p={best_p:.3f}",
            exploratory=False,
            sampled_p=min(1.0, max(0.0, best_p)),
        )

    def reward(
        self,
        category: TaskCategory,
        tier: ModelTier,
        reward: int,
    ) -> None:
        self.state.update(category, tier, reward)

    # Persistence -------------------------------------------------------
    def save(self, path: Path | str) -> None:
        Path(path).write_text(
            json.dumps(self.state.model_dump(), indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path | str, **kwargs: object) -> LLMBandit:
        p = Path(path)
        if not p.exists():
            return cls(**kwargs)
        data = json.loads(p.read_text(encoding="utf-8"))
        state = BanditState.model_validate(data)
        return cls(state=state, **kwargs)
