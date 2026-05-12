"""Tests for causal_attribution — T6 marginal-effect attribution."""
from __future__ import annotations

from typing import Any


def _v2_record(
    consult_id: str = "abc12345",
    school_inputs: dict | None = None,
    hot_weights: dict | None = None,
) -> dict[str, Any]:
    """Build a synthetic v2 trace record for testing."""
    return {
        "consult_id": consult_id,
        "bot_id": "test_bot",
        "schema_version": 2,
        "school_inputs": school_inputs if school_inputs is not None else {
            "momentum": {"score": 0.8, "size_modifier": 0.7},
            "mean_revert": {"score": -0.3, "size_modifier": 0.0},
        },
        "portfolio_inputs": {},
        "hot_weights_snapshot": (
            hot_weights if hot_weights is not None else {"momentum": 1.0, "mean_revert": 1.0}
        ),
        "overrides_snapshot": {},
        "rng_master_seed": None,
        "verdict": {"final_verdict": "PROCEED"},
    }


def test_analyze_detects_decisive_school_for_close_call() -> None:
    """Two schools split 0.56/0.44 → flipping the 0.56 school changes verdict."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    rec = _v2_record(school_inputs={
        "school_a": {"score": 0.56},
        "school_b": {"score": -0.44},  # close to neutral; flip of A flips final
    })
    report = causal_attribution.analyze("abc12345", record=rec)
    assert report.error is None
    assert report.base_verdict == "PROCEED"
    # Flipping school_a by -1.0 should drag final negative
    flipped = next(s for s in report.per_school if s.school == "school_a")
    assert flipped.perturbed_final < 0
    assert flipped.is_decisive
    assert "school_a" in report.decisive_schools


def test_analyze_robust_consensus_no_decisive() -> None:
    """5 strongly-agreeing schools → no single flip changes the verdict."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    rec = _v2_record(school_inputs={
        f"s{i}": {"score": 0.9} for i in range(5)
    })
    report = causal_attribution.analyze("abc12345", record=rec)
    assert report.base_verdict == "PROCEED"
    # Flipping one school -1.0 still leaves 4 at +0.9 → final ≈ +0.62 → PROCEED
    assert report.decisive_schools == []
    for s in report.per_school:
        assert s.is_decisive is False


def test_analyze_returns_error_for_missing_consult() -> None:
    """Non-existent consult_id → empty report with error field."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    report = causal_attribution.analyze("does_not_exist_consult")
    assert report.error is not None
    assert report.decisive_schools == []
    assert report.per_school == []


def test_analyze_rejects_v1_record() -> None:
    """A v1 record (no schema_version) returns the pre-v2 error envelope."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    v1_rec = {
        "consult_id": "v1_consult",
        "bot_id": "old_bot",
        "verdict": {"final_verdict": "PROCEED"},
        # No schema_version, no school_inputs
    }
    report = causal_attribution.analyze("v1_consult", record=v1_rec)
    assert report.error is not None
    assert "pre_v2" in report.error


def test_analyze_handles_empty_school_inputs() -> None:
    """v2 record with empty school_inputs returns error rather than crashing."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    rec = _v2_record(school_inputs={})
    report = causal_attribution.analyze("abc12345", record=rec)
    assert report.error is not None


def test_analyze_handles_missing_consult_id() -> None:
    """Empty consult_id → REJECTED-style error envelope, no exception."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    report = causal_attribution.analyze("")
    assert report.error == "missing_consult_id"


def test_analyze_applies_hot_weights_to_attribution() -> None:
    """Weight of 0 on a school means it can't be decisive."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    rec = _v2_record(
        school_inputs={
            "important": {"score": 0.9},
            "ignored": {"score": -2.0},  # would dominate at weight 1.0
        },
        hot_weights={"important": 1.0, "ignored": 0.0},  # ignored is zero-weighted
    )
    report = causal_attribution.analyze("abc12345", record=rec)
    # Verdict driven entirely by 'important' → PROCEED
    assert report.base_verdict == "PROCEED"
    # Flipping the IGNORED school should NOT change verdict (weight = 0)
    ign = next(s for s in report.per_school if s.school == "ignored")
    assert ign.is_decisive is False


def test_analyze_handles_malformed_school_payload() -> None:
    """Schools with non-numeric scores are skipped, not crashing the report."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    rec = _v2_record(school_inputs={
        "good": {"score": 0.7},
        "garbage": {"score": "not_a_number"},
        "no_score_field": {"size_modifier": 1.0},
    })
    report = causal_attribution.analyze("abc12345", record=rec)
    # Only the 'good' school produces an attribution row
    schools = {s.school for s in report.per_school}
    assert "good" in schools
    # Malformed entries are silently dropped from base_scores extraction


def test_to_dict_serializes_for_mcp_envelope() -> None:
    """CausalReport.to_dict() returns a pure-dict structure usable by MCP envelope."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    rec = _v2_record()
    report = causal_attribution.analyze("abc12345", record=rec)
    d = report.to_dict()
    assert isinstance(d, dict)
    assert "consult_id" in d
    assert isinstance(d["per_school"], list)
    if d["per_school"]:
        assert isinstance(d["per_school"][0], dict)


def test_verdict_label_thresholding() -> None:
    """final_score near zero → HOLD; positive → PROCEED; negative → AVOID."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    assert causal_attribution._verdict_from_score(0.0) == "HOLD"
    assert causal_attribution._verdict_from_score(0.01) == "HOLD"
    assert causal_attribution._verdict_from_score(0.5) == "PROCEED"
    assert causal_attribution._verdict_from_score(-0.5) == "AVOID"


def test_analyze_never_raises_on_corrupt_record() -> None:
    """A wildly malformed record still returns a clean report, never raises."""
    from eta_engine.brain.jarvis_v3 import causal_attribution

    rec = {"schema_version": 2, "school_inputs": "not_a_dict"}  # type: ignore[dict-item]
    report = causal_attribution.analyze("abc12345", record=rec)
    # Should return an error report, NOT raise
    assert isinstance(report, causal_attribution.CausalReport)
    assert report.error is not None
