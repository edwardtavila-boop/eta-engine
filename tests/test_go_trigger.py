"""Tests for scripts.go_trigger — the manual GO/KILL gate."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.scripts import go_trigger as mod

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "preflight_dryrun_report.json").write_text(
        json.dumps({"overall": "GO"}),
    )
    (tmp_path / "roadmap_state.json").write_text(json.dumps({"shared_artifacts": {}}))
    return tmp_path


def test_handle_accepts_known_phrase(fake_root: Path):
    e = mod.handle("GO APEX MNQ LIVE-TINY", reason="first flip")
    assert e.accepted is True
    assert e.action == "flip_live"
    assert e.target == "tier_a_mnq"
    assert e.preflight_verdict == "GO"


def test_handle_rejects_unknown_phrase(fake_root: Path):
    e = mod.handle("go live NOW", reason="")
    assert e.accepted is False
    assert "unknown phrase" in e.reason


def test_handle_blocks_on_preflight_abort(fake_root: Path):
    # Flip preflight to ABORT
    (fake_root / "docs" / "preflight_dryrun_report.json").write_text(
        json.dumps({"overall": "ABORT"}),
    )
    e = mod.handle("GO APEX MNQ LIVE-TINY", reason="")
    assert e.accepted is False
    assert "preflight" in e.reason.lower()


def test_handle_skip_preflight_override(fake_root: Path):
    (fake_root / "docs" / "preflight_dryrun_report.json").write_text(
        json.dumps({"overall": "ABORT"}),
    )
    e = mod.handle("GO APEX MNQ LIVE-TINY", reason="emergency", skip_preflight=True)
    assert e.accepted is True


def test_handle_kill_phrase_does_not_require_preflight(fake_root: Path):
    (fake_root / "docs" / "preflight_dryrun_report.json").write_text(
        json.dumps({"overall": "ABORT"}),
    )
    e = mod.handle("KILL APEX NOW", reason="market gap")
    assert e.accepted is True
    assert e.action == "kill_all"


def test_append_log_writes_jsonl(fake_root: Path):
    e = mod.handle("KILL APEX NOW", reason="test")
    path = mod._append_log(e)
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["phrase"] == "KILL APEX NOW"
    assert parsed["accepted"] is True


def test_patch_roadmap_state_sets_flags(fake_root: Path):
    e = mod.handle("GO APEX MNQ LIVE-TINY", reason="")
    mod._patch_roadmap_state(e)
    state = json.loads((fake_root / "roadmap_state.json").read_text())
    go = state["shared_artifacts"]["apex_go_state"]
    assert go["tier_a_mnq_live"] is True
    assert go["kill_switch_active"] is False


def test_kill_unsets_tier_flags(fake_root: Path):
    # First, go live
    e1 = mod.handle("GO APEX MNQ LIVE-TINY", reason="")
    mod._patch_roadmap_state(e1)
    # Then kill
    e2 = mod.handle("KILL APEX NOW", reason="crash")
    mod._patch_roadmap_state(e2)
    state = json.loads((fake_root / "roadmap_state.json").read_text())
    go = state["shared_artifacts"]["apex_go_state"]
    assert go["kill_switch_active"] is True
    assert go["tier_a_mnq_live"] is False


def test_resume_clears_kill(fake_root: Path):
    mod._patch_roadmap_state(mod.handle("KILL APEX NOW", reason=""))
    mod._patch_roadmap_state(mod.handle("RESUME APEX TIER-A", reason="reviewed"))
    state = json.loads((fake_root / "roadmap_state.json").read_text())
    go = state["shared_artifacts"]["apex_go_state"]
    assert go["kill_switch_active"] is False


def test_preflight_verdict_unknown_when_missing(fake_root: Path):
    (fake_root / "docs" / "preflight_dryrun_report.json").unlink()
    v = mod._preflight_verdict()
    assert v == "UNKNOWN"


def test_case_insensitive_phrase(fake_root: Path):
    e = mod.handle("go apex mnq live-tiny", reason="")
    assert e.accepted is True


def test_phrase_table_covers_all_expected_actions():
    expected = {
        "GO APEX MNQ LIVE-TINY",
        "GO APEX NQ LIVE-TINY",
        "GO APEX BYBIT TESTNET",
        "GO APEX BYBIT MAINNET",
        "KILL APEX NOW",
        "RESUME APEX TIER-A",
        "RESUME APEX TIER-B",
    }
    assert expected.issubset(set(mod.PHRASES.keys()))
