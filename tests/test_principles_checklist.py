"""Tests for core.principles_checklist."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eta_engine.core.principles_checklist import (
    DEFAULT_PRINCIPLES,
    ChecklistAnswer,
    build_report,
    principle_by_index,
    score_to_letter,
)


def _all_yes() -> list[ChecklistAnswer]:
    return [ChecklistAnswer(index=i, yes=True) for i in range(10)]


def _all_no() -> list[ChecklistAnswer]:
    return [ChecklistAnswer(index=i, yes=False) for i in range(10)]


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def test_exactly_10_default_principles() -> None:
    assert len(DEFAULT_PRINCIPLES) == 10


def test_indexes_contiguous() -> None:
    assert [p.index for p in DEFAULT_PRINCIPLES] == list(range(10))


def test_unique_slugs() -> None:
    slugs = [p.slug for p in DEFAULT_PRINCIPLES]
    assert len(slugs) == len(set(slugs))


def test_principle_by_index_lookup() -> None:
    p = principle_by_index(3)
    assert p.slug == "consult_jarvis"


def test_principle_by_index_bad_raises() -> None:
    with pytest.raises(KeyError):
        principle_by_index(42)


# --------------------------------------------------------------------------- #
# score_to_letter
# --------------------------------------------------------------------------- #


def test_score_to_letter_bands() -> None:
    assert score_to_letter(1.0) == "A+"
    assert score_to_letter(0.95) == "A+"
    assert score_to_letter(0.9) == "A"
    assert score_to_letter(0.8) == "B"
    assert score_to_letter(0.7) == "C"
    assert score_to_letter(0.5) == "D"
    assert score_to_letter(0.2) == "F"
    assert score_to_letter(0.0) == "F"


def test_score_to_letter_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        score_to_letter(-0.1)
    with pytest.raises(ValueError):
        score_to_letter(1.1)


# --------------------------------------------------------------------------- #
# ChecklistAnswer
# --------------------------------------------------------------------------- #


def test_answer_requires_valid_index() -> None:
    with pytest.raises(ValidationError):
        ChecklistAnswer(index=10, yes=True)
    with pytest.raises(ValidationError):
        ChecklistAnswer(index=-1, yes=True)


def test_answer_note_stripped() -> None:
    a = ChecklistAnswer(index=0, yes=True, note="  hello  ")
    assert a.note == "hello"


def test_answer_note_max_length() -> None:
    with pytest.raises(ValidationError):
        ChecklistAnswer(index=0, yes=True, note="x" * 501)


# --------------------------------------------------------------------------- #
# build_report
# --------------------------------------------------------------------------- #


def test_build_all_yes_gives_a_plus() -> None:
    r = build_report(_all_yes(), period_label="2026-W15")
    assert r.score == 1.0
    assert r.letter_grade == "A+"
    assert r.discipline_score == 10
    assert r.critical_gaps == []


def test_build_all_no_gives_f() -> None:
    r = build_report(_all_no(), period_label="2026-W15")
    assert r.score == 0.0
    assert r.letter_grade == "F"
    assert r.discipline_score == 0
    assert len(r.critical_gaps) == 10


def test_build_mixed_gives_correct_score() -> None:
    answers = [ChecklistAnswer(index=i, yes=i < 7) for i in range(10)]
    r = build_report(answers, period_label="x")
    assert r.score == 0.7
    assert r.letter_grade == "C"
    assert r.discipline_score == 7
    assert len(r.critical_gaps) == 3


def test_build_rejects_missing_index() -> None:
    answers = [ChecklistAnswer(index=i, yes=True) for i in range(9)]
    with pytest.raises(ValueError, match="missing"):
        build_report(answers, period_label="x")


def test_build_rejects_duplicate_index() -> None:
    answers = [ChecklistAnswer(index=i, yes=True) for i in range(10)] + [ChecklistAnswer(index=0, yes=False)]
    with pytest.raises(ValueError, match="duplicate"):
        build_report(answers, period_label="x")


def test_build_preserves_ts() -> None:
    ts = datetime(2026, 4, 17, 9, 0, tzinfo=UTC)
    r = build_report(_all_yes(), period_label="x", ts=ts)
    assert r.ts == ts


def test_build_critical_gaps_slugs() -> None:
    # Fail exactly risk_discipline (index 7) and override_discipline (index 8)
    answers = [ChecklistAnswer(index=i, yes=i not in {7, 8}) for i in range(10)]
    r = build_report(answers, period_label="x")
    assert set(r.critical_gaps) == {"risk_discipline", "override_discipline"}


def test_failed_slugs_helper() -> None:
    answers = [ChecklistAnswer(index=i, yes=i != 0) for i in range(10)]
    r = build_report(answers, period_label="x")
    assert r.failed_slugs() == ["a_plus_only"]


def test_report_keeps_order_of_answers() -> None:
    # Feed them in a shuffled order; the report should re-sort by index.
    import random

    src = _all_yes()
    random.shuffle(src)
    r = build_report(src, period_label="x")
    assert [a.index for a in r.answers] == list(range(10))
