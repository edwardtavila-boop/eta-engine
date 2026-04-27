"""Tests for the checklist plumbing added to scripts.weekly_review."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.scripts.weekly_review import (
    _load_checklist,
    _write_checklist_report,
    _write_checklist_stub,
)

if TYPE_CHECKING:
    from pathlib import Path


def _answers(all_yes: bool = True) -> list[dict]:
    return [{"index": i, "yes": all_yes, "note": ""} for i in range(10)]


# --------------------------------------------------------------------------- #
# _load_checklist
# --------------------------------------------------------------------------- #


def test_load_none_when_path_missing(tmp_path: Path) -> None:
    assert _load_checklist(tmp_path / "nope.json", "w") is None


def test_load_none_when_path_is_none(tmp_path: Path) -> None:
    assert _load_checklist(None, "w") is None


def test_load_builds_report(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text(json.dumps(_answers(all_yes=True)), encoding="utf-8")
    r = _load_checklist(p, "2026-W15")
    assert r is not None
    assert r.score == 1.0
    assert r.letter_grade == "A+"


def test_load_rejects_non_list(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"bad": "shape"}), encoding="utf-8")
    with pytest.raises(ValueError, match="list"):
        _load_checklist(p, "w")


# --------------------------------------------------------------------------- #
# _write_checklist_stub
# --------------------------------------------------------------------------- #


def test_stub_written_with_10_items(tmp_path: Path) -> None:
    stub_path = _write_checklist_stub(tmp_path / "docs")
    rows = json.loads(stub_path.read_text(encoding="utf-8"))
    assert len(rows) == 10
    assert all(r["yes"] is False for r in rows)
    assert {r["index"] for r in rows} == set(range(10))


# --------------------------------------------------------------------------- #
# _write_checklist_report
# --------------------------------------------------------------------------- #


def test_write_report_produces_json_and_txt(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text(json.dumps(_answers(all_yes=False)), encoding="utf-8")
    r = _load_checklist(p, "2026-W15")
    assert r is not None
    _write_checklist_report(r, tmp_path / "docs")
    assert (tmp_path / "docs" / "weekly_checklist_latest.json").exists()
    assert (tmp_path / "docs" / "weekly_checklist_latest.txt").exists()


def test_write_report_txt_contains_grade(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    p.write_text(json.dumps(_answers(all_yes=True)), encoding="utf-8")
    r = _load_checklist(p, "2026-W15")
    assert r is not None
    _write_checklist_report(r, tmp_path / "docs")
    txt = (tmp_path / "docs" / "weekly_checklist_latest.txt").read_text(
        encoding="utf-8",
    )
    assert "A+" in txt
    assert "Discipline: 10/10" in txt
