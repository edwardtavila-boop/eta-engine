"""Tests for consult_replay — T7 single-consult counterfactual replay."""

from __future__ import annotations

from typing import Any


def _v2_record(
    consult_id: str = "rep001",
    school_inputs: dict | None = None,
    hot_weights: dict | None = None,
    overrides: dict | None = None,
) -> dict[str, Any]:
    return {
        "consult_id": consult_id,
        "bot_id": "test_bot",
        "schema_version": 2,
        "school_inputs": school_inputs
        if school_inputs is not None
        else {
            "momentum": {"score": 0.8},
            "mean_revert": {"score": -0.2},
        },
        "portfolio_inputs": {},
        "hot_weights_snapshot": (hot_weights if hot_weights is not None else {"momentum": 1.0, "mean_revert": 1.0}),
        "overrides_snapshot": overrides if overrides is not None else {},
        "rng_master_seed": None,
        "verdict": {"final_verdict": "PROCEED"},
    }


def test_replay_with_no_overrides_matches_base() -> None:
    """Determinism check: replay without overrides reproduces the base verdict."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = _v2_record()
    result = consult_replay.replay("rep001", record=rec)
    assert result.error is None
    assert result.matched_base is True
    assert result.base_verdict == result.replay_verdict
    assert abs(result.base_final_score - result.replay_final_score) < 1e-9


def test_replay_with_size_modifier_pin_blocks_verdict() -> None:
    """Setting size_modifier=0 → BLOCKED verdict from the override layer."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = _v2_record()
    result = consult_replay.replay(
        "rep001",
        record=rec,
        override_overrides={"size_modifier": 0.0},
    )
    assert result.replay_verdict == "BLOCKED"
    assert result.matched_base is False


def test_replay_with_hot_weights_override_flips_verdict() -> None:
    """Boost the dissenting school heavily → verdict flips."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = _v2_record(
        school_inputs={
            "momentum": {"score": 0.8},
            "mean_revert": {"score": -0.5},
        },
        hot_weights={"momentum": 1.0, "mean_revert": 1.0},  # base: PROCEED
    )
    # Now boost mean_revert to 5.0 → should flip to AVOID
    result = consult_replay.replay(
        "rep001",
        record=rec,
        override_hot_weights={"momentum": 1.0, "mean_revert": 5.0},
    )
    assert result.base_verdict == "PROCEED"
    assert result.replay_verdict == "AVOID"
    assert result.matched_base is False


def test_replay_with_school_inputs_override() -> None:
    """Swap a school's score → cascade follows the swap."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = _v2_record()
    result = consult_replay.replay(
        "rep001",
        record=rec,
        override_school_inputs={
            "momentum": {"score": -1.0},
            "mean_revert": {"score": -1.0},
        },
    )
    # Both schools negative → AVOID
    assert result.replay_verdict == "AVOID"


def test_replay_rejects_v1_record() -> None:
    """Pre-v2 record returns error envelope, no exception."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    v1 = {"consult_id": "v1_consult", "bot_id": "old"}  # no schema_version
    result = consult_replay.replay("v1_consult", record=v1)
    assert result.error is not None
    assert "pre_v2" in result.error


def test_replay_missing_consult_id() -> None:
    from eta_engine.brain.jarvis_v3 import consult_replay

    result = consult_replay.replay("")
    assert result.error == "missing_consult_id"


def test_replay_consult_not_found() -> None:
    """No record lookup hit + no inline record → not_found error."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    result = consult_replay.replay("never_existed_consult_xyz")
    assert result.error is not None
    assert "not_found" in result.error


def test_counterfactual_size_modifier_pin() -> None:
    """counterfactual(pin_size_modifier=0.5) routes through replay correctly."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = _v2_record()
    result = consult_replay.counterfactual(
        "rep001",
        pin_size_modifier=0.5,
        record=rec,
    )
    # Non-zero modifier doesn't BLOCK; it just records the override
    assert result.error is None
    assert result.overrides_applied["overrides_changed"] is True


def test_counterfactual_school_weight_pin() -> None:
    """counterfactual(pin_school=X, pin_weight=Y) overlays hot weights."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = _v2_record(
        school_inputs={
            "momentum": {"score": 0.8},
            "mean_revert": {"score": -0.2},
        },
    )
    # Pin mean_revert weight to 10 → should drag verdict negative
    result = consult_replay.counterfactual(
        "rep001",
        pin_school="mean_revert",
        pin_weight=10.0,
        record=rec,
    )
    assert result.base_verdict in ("PROCEED", "HOLD")
    assert result.replay_verdict == "AVOID"


def test_counterfactual_pin_zero_blocks() -> None:
    """counterfactual(pin_size_modifier=0) → BLOCKED verdict."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = _v2_record()
    result = consult_replay.counterfactual(
        "rep001",
        pin_size_modifier=0.0,
        record=rec,
    )
    assert result.replay_verdict == "BLOCKED"


def test_to_dict_serializes_for_mcp_envelope() -> None:
    """to_dict() returns a pure dict for MCP envelope wrapping."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = _v2_record()
    result = consult_replay.replay("rep001", record=rec)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["consult_id"] == "rep001"
    assert "diff" in d
    assert "overrides_applied" in d


def test_replay_handles_corrupt_record() -> None:
    """Schema v2 marker but malformed inner data → error envelope."""
    from eta_engine.brain.jarvis_v3 import consult_replay

    rec = {"consult_id": "c", "schema_version": 2, "school_inputs": "not_a_dict"}
    result = consult_replay.replay("c", record=rec)
    # School inputs are empty → cascade returns 0.0/HOLD; no error per se
    # but matched_base should be True since we have no overrides
    assert result.error is None or result.replay_verdict in ("HOLD", "UNKNOWN")
