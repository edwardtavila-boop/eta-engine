from __future__ import annotations

import json
from datetime import UTC, datetime

from eta_engine.brain.avengers.base import PersonaId, TaskResult, make_envelope
from eta_engine.brain.avengers.calibration_loop import CalibrationLoop
from eta_engine.brain.model_policy import ModelTier, TaskCategory


def _result(*, persona: PersonaId, success: bool) -> TaskResult:
    return TaskResult(
        task_id="task-1",
        persona_id=persona,
        tier_used=ModelTier.SONNET,
        success=success,
        reason_code="ok" if success else "failed",
        reason="recorded",
        cost_multiplier=1.0,
    )


def test_calibration_loop_records_score_and_appends_journal(tmp_path) -> None:
    journal = tmp_path / "calibration.jsonl"
    loop = CalibrationLoop(journal, rehydrate=False)
    envelope = make_envelope(category=TaskCategory.DEBUG, goal="fix failing parser")

    loop.record(envelope, _result(persona=PersonaId.ALFRED, success=True))
    loop.record(envelope, _result(persona=PersonaId.ALFRED, success=False))

    snapshot = loop.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].successes == 1
    assert snapshot[0].failures == 1
    assert loop.weight(PersonaId.ALFRED, TaskCategory.DEBUG) == 0.5
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2


def test_calibration_loop_rehydrates_valid_records_and_skips_bad_lines(tmp_path) -> None:
    journal = tmp_path / "calibration.jsonl"
    now = datetime(2026, 4, 29, tzinfo=UTC).isoformat()
    journal.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": now,
                        "persona": PersonaId.BATMAN.value,
                        "category": TaskCategory.ADVERSARIAL_REVIEW.value,
                        "success": True,
                    }
                ),
                "{bad json",
                json.dumps({"persona": "", "category": TaskCategory.DEBUG.value, "success": False}),
            ]
        ),
        encoding="utf-8",
    )

    loop = CalibrationLoop(journal, rehydrate=True)
    score = loop.snapshot()[0]

    assert score.persona == PersonaId.BATMAN.value
    assert score.category == TaskCategory.ADVERSARIAL_REVIEW.value
    assert score.last_seen == datetime.fromisoformat(now)
    assert loop.weight(PersonaId.ROBIN, TaskCategory.LOG_PARSING) == 0.5
