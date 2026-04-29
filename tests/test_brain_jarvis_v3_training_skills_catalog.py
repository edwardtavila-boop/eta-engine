from __future__ import annotations

from eta_engine.brain.jarvis_v3.training.skills_catalog import (
    PERSONA_SKILLS,
    can_handle,
    categories_by_persona,
    persona_for_category,
    skills_for,
)
from eta_engine.brain.model_policy import TaskCategory


def test_skills_catalog_returns_case_insensitive_persona_skills() -> None:
    batman = skills_for("batman")

    assert batman == PERSONA_SKILLS["BATMAN"]
    assert len(batman) >= 3
    assert all(skill.title and skill.summary for skill in batman)


def test_skills_catalog_routes_categories_to_expected_personas() -> None:
    assert can_handle("BATMAN", TaskCategory.RED_TEAM_SCORING) is True
    assert can_handle("ALFRED", TaskCategory.TEST_RUN) is True
    assert can_handle("ROBIN", TaskCategory.LOG_PARSING) is True
    assert can_handle("ROBIN", TaskCategory.RED_TEAM_SCORING) is False

    assert persona_for_category(TaskCategory.ARCHITECTURE_DECISION) == "BATMAN"
    assert persona_for_category(TaskCategory.DATA_PIPELINE) == "ALFRED"
    assert persona_for_category(TaskCategory.COMMIT_MESSAGE) == "ROBIN"


def test_categories_by_persona_is_dashboard_safe() -> None:
    categories = categories_by_persona()

    assert categories["BATMAN"][0] == TaskCategory.RED_TEAM_SCORING.value
    assert TaskCategory.STRATEGY_EDIT.value in categories["JARVIS"]
    assert all(isinstance(value, str) for values in categories.values() for value in values)
