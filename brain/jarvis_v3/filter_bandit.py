"""A/B entry-filter bandit (Tier-3 #19, 2026-04-27).

Sibling to the policy-level bandit (``bandit_register_default``).
Operates one level UP-stream: at the SIGNAL stage, BEFORE the JARVIS
verdict.

Use case: "should we add a 50-EMA-rising filter to the cascade_hunter
signal?" -- the policy-level bandit answers "given a signal, what
verdict?". The filter bandit answers "given the same setup, with vs
without filter X, which generates better realized R?".

Different filters become different "arms"; each is evaluated on the
realized R-multiple of the trade that the filter ALLOWED. Filters
that block trades earn a small "patience credit" when the blocked
trade would have been a loser.

Designed to coexist: bots that don't opt in see no behavior change.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = ROOT / "state" / "filter_bandit" / "posterior.json"


@dataclass
class _FilterArm:
    name: str
    callable_fn: Callable[..., bool]  # returns True = pass; False = block
    pulls: int = 0
    rewards: list[float] = field(default_factory=list)

    @property
    def mean_reward(self) -> float:
        return sum(self.rewards) / len(self.rewards) if self.rewards else 0.0


class FilterBandit:
    """Epsilon-greedy bandit over a list of named entry filters.

    Bots that opt in:

        from eta_engine.brain.jarvis_v3.filter_bandit import default_filter_bandit

        fb = default_filter_bandit()
        fb.register("ema_50_rising", ema_50_rising_check)
        fb.register("vol_below_atr_mean", vol_filter)

        passes, used_arm = fb.choose_filter_check(signal=signal)
        if not passes:
            return None

        # Later, when the trade closes:
        fb.observe_outcome(used_arm, realized_r)
    """

    def __init__(
        self,
        *,
        epsilon: float = 0.10,
        state_path: Path = DEFAULT_STATE,
    ) -> None:
        self.epsilon = epsilon
        self.state_path = state_path
        self._lock = threading.Lock()
        self._arms: dict[str, _FilterArm] = {}
        self._null_arm = _FilterArm(name="__null__", callable_fn=lambda **_: True)
        self._load()

    def register(self, name: str, fn: Callable[..., bool]) -> None:
        with self._lock:
            if name in self._arms:
                # Replace the function but keep the existing posterior.
                self._arms[name].callable_fn = fn
            else:
                self._arms[name] = _FilterArm(name=name, callable_fn=fn)

    def choose_filter_check(self, **signal_kwargs: Any) -> tuple[bool, str]:  # noqa: ANN401
        """Pick an arm via epsilon-greedy. Run its filter. Return
        ``(passes, arm_name_used)``."""
        with self._lock:
            arms = list(self._arms.values())
            if not arms:
                return True, "__null__"
            # epsilon-greedy
            arm = random.choice(arms) if random.random() < self.epsilon else max(arms, key=lambda a: a.mean_reward)
        try:
            passes = bool(arm.callable_fn(**signal_kwargs))
        except Exception as exc:  # noqa: BLE001
            logger.warning("filter '%s' raised: %s -- treating as blocked", arm.name, exc)
            passes = False
        return passes, arm.name

    def observe_outcome(self, arm_name: str, realized_r: float) -> None:
        with self._lock:
            arm = self._arms.get(arm_name)
            if arm is None:
                return
            arm.pulls += 1
            arm.rewards.append(float(realized_r))
            # Cap reward history to keep the file small
            if len(arm.rewards) > 500:
                arm.rewards = arm.rewards[-500:]
            self._save()

    def report(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "arm": a.name,
                    "pulls": a.pulls,
                    "mean_r": round(a.mean_reward, 4),
                    "n_rewards": len(a.rewards),
                }
                for a in self._arms.values()
            ]

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            for arm_name, arm_data in data.get("arms", {}).items():
                arm = _FilterArm(
                    name=arm_name,
                    callable_fn=lambda **_: True,  # restored arms are metadata until bots re-register
                    pulls=int(arm_data.get("pulls", 0)),
                    rewards=[float(r) for r in arm_data.get("rewards", [])],
                )
                self._arms[arm_name] = arm
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("filter-bandit load failed (%s); fresh start", exc)

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "arms": {a.name: {"pulls": a.pulls, "rewards": a.rewards} for a in self._arms.values()},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


_default: FilterBandit | None = None


def default_filter_bandit() -> FilterBandit:
    global _default
    if _default is None:
        _default = FilterBandit()
    return _default
