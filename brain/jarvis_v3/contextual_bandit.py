"""Contextual bandit -- per-context arm selection (Tier-4 #18, 2026-04-27).

Replaces the global epsilon-greedy bandit with a CONTEXTUAL bandit
keyed on (regime, session_phase, stress_bucket). Each context-key
maintains its own per-arm reward distribution, so v18 can win the
"high stress + RTH" niche while v20 wins "OVERNIGHT" and v21 wins
"drawdown-near-kill" -- all of them coexisting in the same fleet.

This is an OPT-IN replacement for ``BanditHarness``; the existing
harness keeps working, and consumers flip via env::

    ETA_BANDIT_MODE=contextual          # use ContextualBandit
    ETA_BANDIT_MODE=epsilon  (default)  # legacy global bandit

Algorithm
---------
Per-(context_key, arm_id) Beta-Bernoulli posterior:
  * each "win" (reward > 0) adds 1 to alpha
  * each "loss" (reward < 0) adds 1 to beta
  * each "tie" (reward == 0) is ignored

Thompson sampling per request:
  1. context_key = (regime, session_phase, stress_bucket)
  2. for each arm, sample p ~ Beta(alpha[ctx,arm], beta[ctx,arm])
  3. pick the arm with the highest sample

State persists to ``state/contextual_bandit/posterior.json``.

This is more sample-efficient than per-arm marginal stats because
v18's "advantage in high stress" doesn't dilute v17's "advantage in
calm". Each arm gets the contexts where it's actually better.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "state" / "contextual_bandit" / "posterior.json"
)


def _stress_bucket(composite: float) -> str:
    """Quantize stress_composite to a small label set."""
    if composite < 0.30:
        return "low"
    if composite < 0.55:
        return "med"
    if composite < 0.75:
        return "high"
    return "extreme"


def context_key(
    *,
    regime: str = "unknown",
    session_phase: str = "unknown",
    stress_composite: float = 0.0,
) -> str:
    """Compose the context key the bandit conditions on."""
    return f"{regime}|{session_phase}|{_stress_bucket(stress_composite)}"


@dataclass
class _ArmPosterior:
    alpha: float = 1.0  # priors -- weak Beta(1, 1)
    beta: float  = 1.0
    pulls: int = 0
    last_reward: float = 0.0


class ContextualBandit:
    """Beta-Bernoulli Thompson-sampling contextual bandit.

    Persists to a JSON file per ``state_path``. Thread-safe via lock.
    """

    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        rng: random.Random | None = None,
    ) -> None:
        self.state_path = state_path
        self._lock = threading.Lock()
        self._posterior: dict[tuple[str, str], _ArmPosterior] = {}
        self._rng = rng or random.Random()
        self._registered_arms: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for entry in data.get("posterior", []):
                key = (entry["context_key"], entry["arm_id"])
                self._posterior[key] = _ArmPosterior(
                    alpha=float(entry["alpha"]),
                    beta=float(entry["beta"]),
                    pulls=int(entry.get("pulls", 0)),
                    last_reward=float(entry.get("last_reward", 0.0)),
                )
            self._registered_arms = list(data.get("registered_arms", []))
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            logger.warning("posterior load failed (%s); starting fresh", exc)

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({
                "ts": datetime.now(UTC).isoformat(),
                "registered_arms": self._registered_arms,
                "posterior": [
                    {
                        "context_key": ck,
                        "arm_id": aid,
                        "alpha": p.alpha,
                        "beta": p.beta,
                        "pulls": p.pulls,
                        "last_reward": p.last_reward,
                    }
                    for (ck, aid), p in self._posterior.items()
                ],
            }, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("posterior save failed (%s)", exc)

    def register_arm(self, arm_id: str) -> None:
        with self._lock:
            if arm_id not in self._registered_arms:
                self._registered_arms.append(arm_id)
                self._save()

    def choose_arm(self, *, ctx_key: str) -> str | None:
        """Thompson sampling: pick arm with highest sampled probability.

        Returns None when no arms are registered.
        """
        with self._lock:
            if not self._registered_arms:
                return None
            best_arm: str | None = None
            best_sample = -math.inf
            for arm in self._registered_arms:
                p = self._posterior.get((ctx_key, arm), _ArmPosterior())
                # Beta sample. random.betavariate is in stdlib.
                sample = self._rng.betavariate(p.alpha, p.beta)
                if sample > best_sample:
                    best_sample = sample
                    best_arm = arm
            return best_arm

    def observe_outcome(self, *, ctx_key: str, arm_id: str, reward: float) -> None:
        """Update the (ctx_key, arm_id) posterior with one outcome.

        ``reward`` semantics:
          * > 0  -> win (alpha += 1)
          * < 0  -> loss (beta += 1)
          * == 0 -> ignored
        """
        with self._lock:
            key = (ctx_key, arm_id)
            p = self._posterior.get(key, _ArmPosterior())
            p.pulls += 1
            p.last_reward = float(reward)
            if reward > 0:
                p.alpha += 1
            elif reward < 0:
                p.beta += 1
            self._posterior[key] = p
            self._save()

    def report(self) -> list[dict[str, Any]]:
        """Snapshot of every (ctx_key, arm) posterior + mean estimate."""
        with self._lock:
            out: list[dict[str, Any]] = []
            for (ck, aid), p in sorted(self._posterior.items()):
                # mean of Beta(alpha, beta) = alpha / (alpha + beta)
                mean = p.alpha / (p.alpha + p.beta)
                out.append({
                    "context_key": ck,
                    "arm_id": aid,
                    "alpha": round(p.alpha, 4),
                    "beta": round(p.beta, 4),
                    "pulls": p.pulls,
                    "mean_reward_p": round(mean, 4),
                    "last_reward": round(p.last_reward, 4),
                })
            return out


# Module-level singleton. Lazy.
_default: ContextualBandit | None = None


def default_contextual_bandit() -> ContextualBandit:
    global _default
    if _default is None:
        _default = ContextualBandit()
    return _default


def is_contextual_mode_active() -> bool:
    return os.environ.get("ETA_BANDIT_MODE", "epsilon").lower() == "contextual"
