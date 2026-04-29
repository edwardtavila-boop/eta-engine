from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.brain.avengers.alfred import Alfred
from eta_engine.brain.avengers.base import TaskEnvelope
from eta_engine.brain.avengers.batman import Batman
from eta_engine.brain.avengers.robin import Robin
from eta_engine.brain.model_policy import ModelTier, TaskCategory

if TYPE_CHECKING:
    from pathlib import Path


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        tier: ModelTier,
        system_prompt: str,
        user_prompt: str,
        envelope: TaskEnvelope,
    ) -> str:
        self.calls.append(
            {
                "tier": tier,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "envelope": envelope,
            }
        )
        return f"ok:{tier.value}:{envelope.goal}"


def test_alfred_prompt_keeps_plan_deliverable_check_contract(tmp_path: Path) -> None:
    executor = _RecordingExecutor()
    envelope = TaskEnvelope(category=TaskCategory.DOC_WRITING, goal="update runbook")

    result = Alfred(executor=executor, journal_path=tmp_path / "a.jsonl").dispatch(envelope)

    assert result.success is True
    assert executor.calls[0]["tier"] == ModelTier.SONNET
    prompt = str(executor.calls[0]["system_prompt"])
    assert "You are ALFRED" in prompt
    assert "## Plan" in prompt
    assert "## Deliverable" in prompt
    assert "## Check" in prompt
    assert f"Current task category: {TaskCategory.DOC_WRITING.value}." in prompt


def test_batman_prompt_requires_adversarial_verdict_contract(tmp_path: Path) -> None:
    executor = _RecordingExecutor()
    envelope = TaskEnvelope(
        category=TaskCategory.ADVERSARIAL_REVIEW,
        goal="attack promotion gate",
    )

    result = Batman(executor=executor, journal_path=tmp_path / "b.jsonl").dispatch(envelope)

    assert result.success is True
    assert executor.calls[0]["tier"] == ModelTier.OPUS
    prompt = str(executor.calls[0]["system_prompt"])
    assert "You are BATMAN" in prompt
    assert "## Attack Vectors" in prompt
    assert "## Evidence Check" in prompt
    assert "PROMOTE / ITERATE / KILL" in prompt
    assert f"Current task category: {TaskCategory.ADVERSARIAL_REVIEW.value}." in prompt


def test_robin_prompt_stays_terse_and_mechanical(tmp_path: Path) -> None:
    executor = _RecordingExecutor()
    envelope = TaskEnvelope(category=TaskCategory.COMMIT_MESSAGE, goal="draft commit")

    result = Robin(executor=executor, journal_path=tmp_path / "r.jsonl").dispatch(envelope)

    assert result.success is True
    assert executor.calls[0]["tier"] == ModelTier.HAIKU
    prompt = str(executor.calls[0]["system_prompt"])
    assert "You are ROBIN" in prompt
    assert "## Answer" in prompt
    assert "## Notes" in prompt
    assert "Never add a greeting" in prompt
    assert f"Current task category: {TaskCategory.COMMIT_MESSAGE.value}." in prompt
