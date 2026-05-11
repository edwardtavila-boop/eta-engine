"""Tests for jarvis_v3.hot_learner — within-session per-school weight adaptation (Stream 3)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.brain.jarvis_v3 import hot_learner
from eta_engine.brain.jarvis_v3.hot_learner import (
    CAP_HIGH,
    CAP_LOW,
    MIN_OBSERVATIONS_TO_ACT,
    HotLearnState,
)


@pytest.fixture
def _isolated_state(monkeypatch, tmp_path):
    """Redirect STATE_PATH to a tmp_path so the real state file isn't touched."""
    state_path = tmp_path / "hot_learner.json"
    monkeypatch.setattr(hot_learner, "STATE_PATH", state_path)
    return state_path


def test_initial_state_is_empty(_isolated_state: Path) -> None:
    """`_load()` on missing file returns a default `HotLearnState()`."""
    state = hot_learner._load()
    assert isinstance(state, HotLearnState)
    assert state.weight_mods == {}
    assert state.n_closes_today == 0
    assert state.obs_count_by_school == {}
    assert state.last_decay_ts == ""


def test_observe_close_increments_obs_count(_isolated_state: Path) -> None:
    """Calling observe_close once increments obs_count_by_school for the involved school."""
    hot_learner.observe_close(
        asset="BTC",
        school_attribution={"order_flow": 1.0},
        r_outcome=1.0,
    )
    state = hot_learner._load()
    assert state.obs_count_by_school.get("BTC:order_flow") == 1


def test_observe_close_below_threshold_no_weights_returned(_isolated_state: Path) -> None:
    """With observations < MIN_OBSERVATIONS_TO_ACT, current_weights omits the school."""
    for _ in range(MIN_OBSERVATIONS_TO_ACT - 1):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"order_flow": 1.0},
            r_outcome=1.0,
        )
    weights = hot_learner.current_weights("BTC")
    assert "order_flow" not in weights


def test_observe_close_above_threshold_returns_weight(_isolated_state: Path) -> None:
    """After >= MIN_OBSERVATIONS_TO_ACT observations, the school weight is exposed and in caps."""
    for _ in range(MIN_OBSERVATIONS_TO_ACT):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"order_flow": 1.0},
            r_outcome=1.0,
        )
    weights = hot_learner.current_weights("BTC")
    assert "order_flow" in weights
    assert CAP_LOW <= weights["order_flow"] <= CAP_HIGH


def test_negative_attribution_with_loss_increases_weight(_isolated_state: Path) -> None:
    """attribution=-1 (school voted against) and r_outcome=-1 (loss) → signed_reward = +1 → weight up."""
    for _ in range(20):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"wyckoff": -1.0},
            r_outcome=-1.0,
        )
    weights = hot_learner.current_weights("BTC")
    assert "wyckoff" in weights
    assert weights["wyckoff"] > 1.0


def test_positive_attribution_with_loss_decreases_weight(_isolated_state: Path) -> None:
    """attribution=+1 (school voted for) and r_outcome=-1 (loss) → signed_reward = -1 → weight down."""
    for _ in range(20):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"wyckoff": 1.0},
            r_outcome=-1.0,
        )
    weights = hot_learner.current_weights("BTC")
    assert "wyckoff" in weights
    assert weights["wyckoff"] < 1.0


def test_weight_capped_at_low(_isolated_state: Path) -> None:
    """Many negative-signed-reward observations: weight bottoms at CAP_LOW, never below."""
    for _ in range(500):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"wyckoff": 1.0},
            r_outcome=-1.0,
        )
    weights = hot_learner.current_weights("BTC")
    assert weights["wyckoff"] == pytest.approx(CAP_LOW, abs=1e-9)
    assert weights["wyckoff"] >= CAP_LOW


def test_weight_capped_at_high(_isolated_state: Path) -> None:
    """Many positive-signed-reward observations: weight tops at CAP_HIGH, never above."""
    for _ in range(500):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"order_flow": 1.0},
            r_outcome=1.0,
        )
    weights = hot_learner.current_weights("BTC")
    assert weights["order_flow"] == pytest.approx(CAP_HIGH, abs=1e-9)
    assert weights["order_flow"] <= CAP_HIGH


def test_decay_moves_toward_1(_isolated_state: Path) -> None:
    """Setting weight to 1.5 and decaying once → new weight = 0.7 * 1.5 + 0.3 * 1.0 = 1.35."""
    state = HotLearnState(
        weight_mods={"BTC": {"order_flow": 1.5}},
        n_closes_today=5,
        obs_count_by_school={"BTC:order_flow": 5},
    )
    hot_learner._save(state)
    hot_learner.decay_overnight()
    decayed = hot_learner._load()
    assert decayed.weight_mods["BTC"]["order_flow"] == pytest.approx(1.35, abs=1e-9)


def test_decay_resets_n_closes_today(_isolated_state: Path) -> None:
    """After decay_overnight: n_closes_today = 0 and obs_count_by_school is cleared."""
    for _ in range(7):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"order_flow": 1.0},
            r_outcome=1.0,
        )
    pre = hot_learner._load()
    assert pre.n_closes_today > 0
    hot_learner.decay_overnight()
    post = hot_learner._load()
    assert post.n_closes_today == 0
    assert post.obs_count_by_school == {}
    assert post.last_decay_ts != ""


def test_per_asset_segmentation(_isolated_state: Path) -> None:
    """BTC observations do not leak into MNQ weights."""
    for _ in range(MIN_OBSERVATIONS_TO_ACT):
        hot_learner.observe_close(
            asset="BTC",
            school_attribution={"order_flow": 1.0},
            r_outcome=1.0,
        )
    mnq_weights = hot_learner.current_weights("MNQ")
    assert mnq_weights == {}


def test_malformed_state_file_returns_default(_isolated_state: Path) -> None:
    """Bad JSON on STATE_PATH → _load returns default state, never raises."""
    _isolated_state.parent.mkdir(parents=True, exist_ok=True)
    _isolated_state.write_text("not: a: valid: json :{{{")
    state = hot_learner._load()
    assert isinstance(state, HotLearnState)
    assert state.weight_mods == {}
    assert state.n_closes_today == 0
    assert state.obs_count_by_school == {}


def test_save_persists_to_disk(_isolated_state: Path) -> None:
    """Saved state lands on disk as readable JSON (sanity check)."""
    state = HotLearnState(
        weight_mods={"BTC": {"order_flow": 1.2}},
        n_closes_today=4,
        obs_count_by_school={"BTC:order_flow": 4},
        last_decay_ts="2026-05-11T00:00:00+00:00",
    )
    hot_learner._save(state)
    assert _isolated_state.exists()
    raw = json.loads(_isolated_state.read_text())
    assert raw["weight_mods"] == {"BTC": {"order_flow": 1.2}}
    assert raw["n_closes_today"] == 4
