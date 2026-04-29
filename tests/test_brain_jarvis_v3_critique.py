from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from eta_engine.brain.jarvis_v3.critique import DecisionRecord, critique_window, load_decisions


def _decision(
    now: datetime,
    minutes: int,
    verdict: str,
    correct: int,
    stress: float,
    *,
    realized_r: float | None = None,
    counterfactual_r: float | None = None,
) -> DecisionRecord:
    return DecisionRecord(
        ts=now - timedelta(minutes=minutes),
        verdict=verdict,
        reason_code="test",
        stress_composite=stress,
        outcome_correct=correct,
        realized_r=realized_r,
        counterfactual_r=counterfactual_r,
    )


def test_critique_window_flags_red_false_positive_and_stress_drift() -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)
    decisions = [
        _decision(now, 90, "APPROVED", 1, 0.1, realized_r=1.0),
        _decision(now, 80, "DENIED", 1, 0.2, counterfactual_r=-0.5),
        _decision(now, 70, "APPROVED", 0, 0.7, realized_r=-1.2),
        _decision(now, 60, "APPROVED", 0, 0.8, realized_r=-1.5),
    ]

    report = critique_window(decisions, window_days=1, now=now)

    assert report.total_decisions == 4
    assert report.approved_wrong == 2
    assert report.false_positive_rate == 0.6667
    assert report.stress_drift == 0.6
    assert report.severity == "RED"
    assert "operator review required" in report.recommendation


def test_load_decisions_skips_bad_json_and_invalid_records(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    now = datetime(2026, 4, 29, tzinfo=UTC)
    valid = _decision(now, 0, "DENIED", 0, 0.4).model_dump(mode="json")
    path.write_text(
        "\n".join(
            [
                json.dumps(valid),
                "{bad json",
                json.dumps({"ts": now.isoformat(), "verdict": "", "stress_composite": 0.1}),
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_decisions(path)

    assert len(loaded) == 1
    assert loaded[0].verdict == "DENIED"
    assert load_decisions(tmp_path / "missing.jsonl") == []
