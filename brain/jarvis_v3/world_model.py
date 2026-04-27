"""Lightweight world model for trade rollout / 'dreaming' (Wave-8 #1).

What the audit asked for: latent + diffusion-based Dreamer-style
internal simulator that can imagine 50 paths before committing a trade.

What we ship: a Markov-chain-over-discretized-states scaffold. Pure
stdlib. NO PyTorch. Trained from journaled (state, next_state)
transitions. The point is to start the loop and harvest the value
NOW; a deep latent model is a drop-in upgrade later.

How it works:

  1. Bucket each market state into a small finite alphabet using a
     hand-rolled encoder (regime+session+vol -> int)
  2. Learn empirical transition probabilities P(s_{t+1} | s_t, a_t)
     from past episodes
  3. At decision time: from the current state, run K rollouts of
     length H, sampling next-states from the learned transition
     probabilities, and score each rollout by cumulative reward
  4. Return aggregate statistics: pct_paths_profitable, avg_terminal_r,
     worst_terminal_r, best_terminal_r

Use case (pre-trade dreaming):

    from eta_engine.brain.jarvis_v3.world_model import dream

    report = dream(
        current_state=encode_state(regime="bullish_low_vol", session="rth", vol=0.4),
        proposed_action="approve_full",
        n_paths=50,
        horizon=8,
        memory=hierarchical_memory,
    )
    if report.pct_paths_profitable < 0.40:
        # Imagined futures don't favor this trade
        ...

Scaffold limitations (intentionally narrow):
  * Discrete state space (4 regimes x 3 sessions x 3 vol buckets = 36)
  * Markov-1 (no longer history)
  * Reward sampled from past observed R conditional on state
  * No action conditioning beyond observed action-tagged episodes
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.brain.jarvis_v3.memory_hierarchy import (
        Episode,
        HierarchicalMemory,
    )

logger = logging.getLogger(__name__)


# ─── State encoding ───────────────────────────────────────────────


_REGIME_BUCKETS = {
    "bearish_high_vol": 0, "bearish_low_vol": 1,
    "neutral": 2, "bullish_low_vol": 3, "bullish_high_vol": 4,
}
_SESSION_BUCKETS = {
    "overnight": 0, "premarket": 1, "rth": 2, "afterhours": 1,
}


def encode_state(*, regime: str, session: str, stress: float) -> int:
    """Hand-rolled state encoder. Returns an integer in a small
    finite alphabet."""
    r = _REGIME_BUCKETS.get(regime.lower(), 2)
    s = _SESSION_BUCKETS.get(session.lower(), 0)
    if stress < 0.33:
        v = 0
    elif stress < 0.67:
        v = 1
    else:
        v = 2
    # 5 regimes * 3 sessions * 3 vol = 45 states max
    return r * 9 + s * 3 + v


def describe_state(state_id: int) -> str:
    r = state_id // 9
    s = (state_id // 3) % 3
    v = state_id % 3
    regime_label = next((k for k, vv in _REGIME_BUCKETS.items() if vv == r), "neutral")
    session_label = ["overnight", "premarket/afterhours", "rth"][s]
    vol_label = ["low_vol", "med_vol", "high_vol"][v]
    return f"{regime_label}/{session_label}/{vol_label}"


# ─── Transition table ─────────────────────────────────────────────


@dataclass
class TransitionTable:
    """Empirical P(next | current) and reward conditional on state."""

    transitions: dict[int, dict[int, int]] = field(default_factory=dict)
    rewards_by_state: dict[int, list[float]] = field(default_factory=dict)

    def fit_from_episodes(self, episodes: list[Episode]) -> None:
        """Populate transitions + rewards from a sequence of past
        episodes. Episodes are taken in time order; each consecutive
        pair contributes a transition."""
        for i, ep in enumerate(episodes):
            s = encode_state(
                regime=ep.regime, session=ep.session, stress=ep.stress,
            )
            self.rewards_by_state.setdefault(s, []).append(ep.realized_r)
            if i + 1 < len(episodes):
                nxt_ep = episodes[i + 1]
                s_next = encode_state(
                    regime=nxt_ep.regime, session=nxt_ep.session,
                    stress=nxt_ep.stress,
                )
                row = self.transitions.setdefault(s, {})
                row[s_next] = row.get(s_next, 0) + 1

    def sample_next(self, state: int, rng: random.Random) -> int:
        row = self.transitions.get(state)
        if not row:
            return state  # absorbing state when unobserved
        total = sum(row.values())
        u = rng.uniform(0, total)
        acc = 0
        for next_s, count in row.items():
            acc += count
            if u <= acc:
                return next_s
        return state

    def sample_reward(self, state: int, rng: random.Random) -> float:
        rs = self.rewards_by_state.get(state)
        if not rs:
            return 0.0
        return rng.choice(rs)


# ─── Rollouts (the "dreaming" step) ───────────────────────────────


@dataclass
class DreamReport:
    """Output of a dream: aggregate stats over the rollouts."""

    n_paths: int
    horizon: int
    starting_state: int
    starting_state_label: str
    pct_paths_profitable: float
    avg_terminal_r: float
    median_terminal_r: float
    worst_terminal_r: float
    best_terminal_r: float
    pct_paths_blown_up: float    # % of paths with cum R <= -2
    sample_paths: list[list[float]] = field(default_factory=list)


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = max(0, min(len(s) - 1, int(p * len(s))))
    return s[idx]


def dream(
    *,
    current_state: int,
    n_paths: int = 50,
    horizon: int = 8,
    memory: HierarchicalMemory,
    transitions: TransitionTable | None = None,
    blow_up_threshold_r: float = -2.0,
    keep_sample_paths: int = 5,
    rng: random.Random | None = None,
) -> DreamReport:
    """Roll out ``n_paths`` length-``horizon`` futures from the current
    state, using transitions learned from memory if not supplied.

    Returns aggregate statistics. The intent is for the caller to
    consult these BEFORE committing to a trade; e.g. defer if
    ``pct_paths_profitable < 0.4`` or ``pct_paths_blown_up > 0.2``.
    """
    rng = rng or random.Random()
    table = transitions or TransitionTable()
    if not table.transitions:
        table.fit_from_episodes(memory._episodes)

    paths_terminal_r: list[float] = []
    sample_paths: list[list[float]] = []
    blown_up = 0
    for path_idx in range(n_paths):
        s = current_state
        cum_r = 0.0
        per_step: list[float] = []
        for _ in range(horizon):
            r = table.sample_reward(s, rng)
            cum_r += r
            per_step.append(round(cum_r, 4))
            s = table.sample_next(s, rng)
        paths_terminal_r.append(cum_r)
        if cum_r <= blow_up_threshold_r:
            blown_up += 1
        if path_idx < keep_sample_paths:
            sample_paths.append(per_step)

    if not paths_terminal_r:
        return DreamReport(
            n_paths=0, horizon=horizon, starting_state=current_state,
            starting_state_label=describe_state(current_state),
            pct_paths_profitable=0.0,
            avg_terminal_r=0.0, median_terminal_r=0.0,
            worst_terminal_r=0.0, best_terminal_r=0.0,
            pct_paths_blown_up=0.0,
        )
    pct_profitable = sum(1 for r in paths_terminal_r if r > 0) / len(paths_terminal_r)
    return DreamReport(
        n_paths=len(paths_terminal_r),
        horizon=horizon,
        starting_state=current_state,
        starting_state_label=describe_state(current_state),
        pct_paths_profitable=round(pct_profitable, 3),
        avg_terminal_r=round(sum(paths_terminal_r) / len(paths_terminal_r), 4),
        median_terminal_r=round(_percentile(paths_terminal_r, 0.50), 4),
        worst_terminal_r=round(min(paths_terminal_r), 4),
        best_terminal_r=round(max(paths_terminal_r), 4),
        pct_paths_blown_up=round(blown_up / len(paths_terminal_r), 3),
        sample_paths=sample_paths,
    )
