"""ETA Engine RL trading environment (Tier-1 #1, scaffold, 2026-04-27).

A Gym-like trading environment that lets a PPO agent (or any RL
policy) train on the burn-in journal as if it were a market. The
agent's action space is JARVIS's action space (APPROVED / CONDITIONAL
with cap / DEFERRED / DENIED), the state is a trimmed JarvisContext
projection, and the reward is a trade-realized R-multiple minus
drawdown penalty.

This is a SCAFFOLD: it depends on stable-baselines3 + gymnasium which
operator can install via ``pip install stable-baselines3 gymnasium``.
The environment is functional WITHOUT those libs (operator can use the
state/action/reward primitives in any RL framework).

Reward design
-------------
  reward = realized_R_of_trade - drawdown_penalty * |dd_increment|

The DD penalty is the operator-tunable knob: higher = more risk-averse
agents. Default 0.5 means a 1R drawdown costs the agent 0.5R of credit.

State design (trimmed for sample efficiency)
--------------------------------------------
  * stress_composite (0..1)
  * session_phase (one-hot 7-class)
  * recent_drawdown_pct (0..1)
  * last_3_R_multiples
  * minutes_to_next_macro_event (clipped 0..480)

Total state dim: ~17 floats (compact enough for fast PPO convergence).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class TradeStep:
    """One state -> action -> reward transition."""

    state: list[float]
    action: int
    reward: float
    next_state: list[float]
    done: bool
    info: dict = field(default_factory=dict)


# Action space (mirrors JARVIS Verdict + size cap)
class RLAction:
    APPROVED_FULL = 0  # APPROVED, no cap
    APPROVED_HALF = 1  # CONDITIONAL with cap=0.5
    APPROVED_QUARTER = 2  # CONDITIONAL with cap=0.25
    DEFERRED = 3
    DENIED = 4

    N = 5


@dataclass
class EtaTradingEnvSpec:
    """Configuration for the RL environment."""

    burn_in_journal: Path = ROOT / "data" / "burn_in" / "journal.sqlite"
    drawdown_penalty_coef: float = 0.5
    starting_equity: float = 10_000.0
    risk_per_trade: float = 100.0  # 1% of starting equity
    max_steps: int = 500  # episodes capped
    seed: int = 42


class EtaTradingEnv:
    """Stand-alone Gym-compatible trading env. Doesn't IMPORT gym so
    the package stays optional; the interface mimics gym.Env so a
    stable-baselines3 wrapper is one adapter line away."""

    def __init__(self, spec: EtaTradingEnvSpec | None = None) -> None:
        self.spec = spec or EtaTradingEnvSpec()
        self._rng = random.Random(self.spec.seed)
        self._equity = self.spec.starting_equity
        self._peak_equity = self.spec.starting_equity
        self._step_count = 0
        self._r_history: list[float] = []
        self._loaded_samples: list[dict] = []

    # ---------- gym-style API ----------

    def reset(self) -> list[float]:
        self._equity = self.spec.starting_equity
        self._peak_equity = self.spec.starting_equity
        self._step_count = 0
        self._r_history = []
        self._loaded_samples = self._load_episode_samples()
        return self._observe(0)

    def step(self, action: int) -> tuple[list[float], float, bool, dict]:
        if action not in range(RLAction.N):
            raise ValueError(f"action must be in 0..{RLAction.N - 1}, got {action}")

        sample = self._loaded_samples[self._step_count] if self._step_count < len(self._loaded_samples) else None
        ground_truth_r = sample.get("realized_r", 0.0) if sample else 0.0

        # Compute reward based on action choice
        reward, terminal_info = self._compute_reward(action, ground_truth_r)

        # Update equity track
        self._equity += reward * self.spec.risk_per_trade
        if self._equity > self._peak_equity:
            self._peak_equity = self._equity
        self._r_history.append(ground_truth_r)

        self._step_count += 1
        done = self._step_count >= self.spec.max_steps or self._equity <= 0.5 * self.spec.starting_equity  # blow-up
        next_state = self._observe(self._step_count)
        info = {"equity": self._equity, "peak": self._peak_equity, "ground_truth_r": ground_truth_r, **terminal_info}
        return next_state, reward, done, info

    # ---------- internals ----------

    def _load_episode_samples(self) -> list[dict]:
        """Load journal events for one bootstrap episode. Falls back to
        synthetic samples when burn-in journal isn't available."""
        # SCAFFOLD: real impl would query the SQLite burn-in journal.
        # For now, return synthetic samples with realized_r drawn from
        # a +0.3 expectation distribution.
        return [{"realized_r": self._rng.gauss(0.3, 1.2)} for _ in range(self.spec.max_steps)]

    def _observe(self, step_idx: int) -> list[float]:
        """Trimmed observation vector (~17 floats)."""
        # Defaults when no episode in flight
        obs = [0.0] * 17
        obs[0] = self._equity / self.spec.starting_equity
        peak_dd_pct = (self._peak_equity - self._equity) / max(self._peak_equity, 1.0)
        obs[1] = peak_dd_pct
        # Last 3 realized R-multiples (padded with 0)
        for i in range(3):
            idx = -1 - i
            if abs(idx) <= len(self._r_history):
                obs[2 + i] = self._r_history[idx]
        # Step progress
        obs[5] = step_idx / self.spec.max_steps
        # Remaining 11 slots reserved for stress / session / macro features
        # populated by the live wrapper that consults JarvisContext.
        return obs

    def _compute_reward(self, action: int, ground_truth_r: float) -> tuple[float, dict]:
        """Reward = action_outcome_R - dd_penalty.

        Skipping a losing trade (action=DEFERRED on a -1R sample) earns
        the agent a small positive credit; taking it earns the loss.
        Approving a winning trade earns the realized R; size-capping
        scales the realized R proportionally.
        """
        if action == RLAction.APPROVED_FULL:
            r_credited = ground_truth_r
        elif action == RLAction.APPROVED_HALF:
            r_credited = ground_truth_r * 0.5
        elif action == RLAction.APPROVED_QUARTER:
            r_credited = ground_truth_r * 0.25
        elif action == RLAction.DEFERRED:
            # Skipped trade -- earn a small "patience" credit when the
            # skipped trade was actually a loser
            r_credited = -ground_truth_r * 0.10
        else:  # DENIED
            r_credited = -ground_truth_r * 0.10

        # Drawdown penalty
        peak_dd_now = (self._peak_equity - self._equity) / max(self._peak_equity, 1.0)
        dd_penalty = self.spec.drawdown_penalty_coef * peak_dd_now * abs(r_credited)
        reward = r_credited - dd_penalty

        return round(reward, 4), {
            "ground_truth_r": ground_truth_r,
            "r_credited": round(r_credited, 4),
            "dd_penalty": round(dd_penalty, 4),
        }


def quick_smoke() -> dict:
    """Run a short random-policy episode for sanity check."""
    env = EtaTradingEnv(EtaTradingEnvSpec(max_steps=100))
    state = env.reset()
    rng = random.Random(0)
    total = 0.0
    for _ in range(100):
        a = rng.randint(0, RLAction.N - 1)
        state, r, done, info = env.step(a)
        total += r
        if done:
            break
    return {
        "total_reward": round(total, 4),
        "final_equity": round(info["equity"], 2),
        "peak_equity": round(info["peak"], 2),
    }
