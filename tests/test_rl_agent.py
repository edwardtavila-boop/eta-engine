"""RL agent baseline tests — P10_AI ppo_sac_agent."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.brain.regime import RegimeType
from eta_engine.brain.rl_agent import RLAction, RLAgent, RLState

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# RLState + RLAction types
# ---------------------------------------------------------------------------


def test_rl_state_defaults() -> None:
    s = RLState(features=[0.1, 0.2])
    assert s.regime == RegimeType.TRANSITION
    assert s.confluence_score == 0.0
    assert s.position_pnl == 0.0


def test_rl_state_enforces_confluence_score_bounds() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RLState(features=[], confluence_score=11.0)


def test_rl_action_enum_covers_expected_actions() -> None:
    names = {a.value for a in RLAction}
    assert names == {
        "LONG",
        "SHORT",
        "HOLD",
        "CLOSE",
        "INCREASE_SIZE",
        "DECREASE_SIZE",
    }


# ---------------------------------------------------------------------------
# RLAgent.select_action — stub policy is deterministic given the seed
# ---------------------------------------------------------------------------


def test_select_action_is_deterministic_under_fixed_seed() -> None:
    a = RLAgent(seed=123)
    b = RLAgent(seed=123)
    state = RLState(features=[0.0], confluence_score=5.0)
    seq_a = [a.select_action(state) for _ in range(20)]
    seq_b = [b.select_action(state) for _ in range(20)]
    assert seq_a == seq_b


def test_select_action_biases_hold_when_confluence_low() -> None:
    # HOLD is 70% weighted when confluence < 4.0. Over 200 draws the mode
    # should land on HOLD.
    agent = RLAgent(seed=7)
    state = RLState(features=[0.0], confluence_score=1.0)
    counts: dict[RLAction, int] = {}
    for _ in range(200):
        act = agent.select_action(state)
        counts[act] = counts.get(act, 0) + 1
    # HOLD should dominate
    top = max(counts, key=lambda k: counts[k])
    assert top == RLAction.HOLD
    assert counts[RLAction.HOLD] / 200 >= 0.55


def test_select_action_favors_directional_when_confluence_high() -> None:
    # LONG+SHORT are each 30% when confluence >= 7.0, so directional combined
    # should be majority.
    agent = RLAgent(seed=11)
    state = RLState(features=[0.0], confluence_score=9.0)
    counts: dict[RLAction, int] = {}
    for _ in range(300):
        act = agent.select_action(state)
        counts[act] = counts.get(act, 0) + 1
    directional = counts.get(RLAction.LONG, 0) + counts.get(RLAction.SHORT, 0)
    assert directional / 300 >= 0.50


def test_select_action_increments_step_count() -> None:
    agent = RLAgent(seed=0)
    state = RLState(features=[0.0])
    agent.select_action(state)
    agent.select_action(state)
    agent.select_action(state)
    # Private attr, but the save_model persists step count — verify via saver
    assert agent._step_count == 3


# ---------------------------------------------------------------------------
# RLAgent.update — replay buffer
# ---------------------------------------------------------------------------


def test_update_stores_experience_in_replay_buffer() -> None:
    agent = RLAgent(seed=0)
    state = RLState(features=[0.1])
    agent.update(state, RLAction.LONG, reward=1.25)
    agent.update(state, RLAction.HOLD, reward=0.0)
    assert len(agent._replay_buffer) == 2
    assert agent._replay_buffer[0][1] == RLAction.LONG
    assert agent._replay_buffer[0][2] == 1.25


# ---------------------------------------------------------------------------
# RLAgent.save_model + load_model
# ---------------------------------------------------------------------------


def test_save_model_writes_metadata_json(tmp_path: Path) -> None:
    agent = RLAgent(seed=0)
    state = RLState(features=[0.1])
    for _ in range(5):
        agent.select_action(state)
    agent.update(state, RLAction.HOLD, 0.0)
    agent.update(state, RLAction.HOLD, 0.0)

    out = tmp_path / "weights" / "rl_model.json"
    agent.save_model(out)

    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["steps"] == 5
    assert payload["buffer_size"] == 2
    assert payload["type"] == "random_baseline"


def test_save_model_creates_parent_directories(tmp_path: Path) -> None:
    agent = RLAgent(seed=0)
    nested = tmp_path / "a" / "b" / "c" / "model.json"
    agent.save_model(nested)
    assert nested.exists()


def test_load_model_restores_step_count(tmp_path: Path) -> None:
    agent = RLAgent(seed=0)
    state = RLState(features=[0.1])
    for _ in range(7):
        agent.select_action(state)
    out = tmp_path / "model.json"
    agent.save_model(out)

    fresh = RLAgent(seed=0)
    assert fresh._step_count == 0
    fresh.load_model(out)
    assert fresh._step_count == 7


def test_load_model_raises_when_file_missing(tmp_path: Path) -> None:
    agent = RLAgent(seed=0)
    with pytest.raises(FileNotFoundError):
        agent.load_model(tmp_path / "does_not_exist.json")
