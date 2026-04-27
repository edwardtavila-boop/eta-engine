"""
EVOLUTIONARY TRADING ALGO  //  brain.rl_agent
=================================
Reinforcement learning agent stub.
Skeleton for PPO/SAC integration. Currently random baseline.
"""

from __future__ import annotations

import json
import random
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from eta_engine.brain.regime import RegimeType

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class RLAction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    INCREASE_SIZE = "INCREASE_SIZE"
    DECREASE_SIZE = "DECREASE_SIZE"


class RLState(BaseModel):
    """Observable state vector for the RL agent."""

    features: list[float] = Field(description="Normalized feature vector")
    regime: RegimeType = RegimeType.TRANSITION
    confluence_score: float = Field(default=0.0, ge=0.0, le=10.0)
    position_pnl: float = 0.0


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class RLAgent:
    """RL agent with pluggable policy.

    Current implementation: random baseline.
    TODO: PPO via stable-baselines3 or custom SAC with PyTorch.

    The interface is frozen — swap internals, keep the API.
    """

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._replay_buffer: list[tuple[RLState, RLAction, float]] = []
        self._step_count: int = 0

    def select_action(self, state: RLState) -> RLAction:
        """Choose an action given current state.

        Production: forward pass through policy network.
        Stub: random action weighted by confluence score.
        """
        self._step_count += 1

        # Bias toward HOLD when confluence is low
        if state.confluence_score < 4.0:
            weights = [0.05, 0.05, 0.70, 0.10, 0.05, 0.05]
        elif state.confluence_score >= 7.0:
            weights = [0.30, 0.30, 0.10, 0.10, 0.10, 0.10]
        else:
            weights = [0.15, 0.15, 0.30, 0.15, 0.125, 0.125]

        actions = list(RLAction)
        return self._rng.choices(actions, weights=weights, k=1)[0]

    def update(self, state: RLState, action: RLAction, reward: float) -> None:
        """Store experience and update policy.

        Production: add to replay buffer, train on mini-batch.
        Stub: append to in-memory buffer.
        """
        self._replay_buffer.append((state, action, reward))
        # TODO: PPO/SAC gradient update every N steps

    def save_model(self, path: str | Path) -> None:
        """Serialize model weights to disk.

        Production: torch.save(policy.state_dict(), path)
        Stub: save metadata JSON.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "steps": self._step_count,
            "buffer_size": len(self._replay_buffer),
            "type": "random_baseline",
        }
        p.write_text(json.dumps(meta, indent=2))

    def load_model(self, path: str | Path) -> None:
        """Load model weights from disk.

        Production: policy.load_state_dict(torch.load(path))
        Stub: load metadata and reset step count.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Model not found at {p}")
        meta = json.loads(p.read_text())
        self._step_count = meta.get("steps", 0)
