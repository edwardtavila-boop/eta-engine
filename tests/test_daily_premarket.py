"""Tests for scripts.daily_premarket."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from eta_engine.brain.jarvis_context import ActionSuggestion
from eta_engine.scripts.daily_premarket import _render_text, run

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Inputs fixture
# --------------------------------------------------------------------------- #


def _green_inputs() -> dict:
    return {
        "macro": {
            "vix_level": 14.5,
            "macro_bias": "neutral",
        },
        "equity": {
            "account_equity": 50_000.0,
            "daily_pnl": 120.0,
            "daily_drawdown_pct": 0.0,
            "open_positions": 0,
            "open_risk_r": 0.0,
        },
        "regime": {
            "regime": "TRENDING_UP",
            "confidence": 0.85,
            "flipped_recently": False,
        },
        "journal": {
            "kill_switch_active": False,
            "autopilot_mode": "ACTIVE",
            "overrides_last_24h": 0,
            "blocked_last_24h": 1,
            "executed_last_24h": 5,
        },
    }


# --------------------------------------------------------------------------- #
# run()
# --------------------------------------------------------------------------- #


def test_run_produces_three_outputs(tmp_path: Path) -> None:
    inputs = tmp_path / "premarket_inputs.json"
    inputs.write_text(json.dumps(_green_inputs()), encoding="utf-8")
    out_dir = tmp_path / "docs"

    ctx = run(inputs_path=inputs, out_dir=out_dir)
    assert (out_dir / "premarket_latest.json").exists()
    assert (out_dir / "premarket_latest.txt").exists()
    assert (out_dir / "premarket_log.jsonl").exists()
    assert ctx.suggestion.action == ActionSuggestion.TRADE


def test_run_stubs_when_inputs_missing(tmp_path: Path) -> None:
    inputs = tmp_path / "nope.json"
    out_dir = tmp_path / "docs"
    ctx = run(inputs_path=inputs, out_dir=out_dir)
    assert any("stub" in n.lower() or "premarket_inputs" in n.lower() for n in ctx.notes)


def test_run_appends_to_log(tmp_path: Path) -> None:
    inputs = tmp_path / "p.json"
    inputs.write_text(json.dumps(_green_inputs()), encoding="utf-8")
    out_dir = tmp_path / "docs"
    run(inputs_path=inputs, out_dir=out_dir)
    run(inputs_path=inputs, out_dir=out_dir)
    log_lines = (
        (out_dir / "premarket_log.jsonl")
        .read_text(
            encoding="utf-8",
        )
        .strip()
        .split("\n")
    )
    assert len(log_lines) == 2


def test_run_with_fomc_inputs_suggests_stand_aside(tmp_path: Path) -> None:
    inputs_data = _green_inputs()
    inputs_data["macro"]["next_event_label"] = "FOMC 2026-05-01 14:00 ET"
    inputs_data["macro"]["hours_until_next_event"] = 0.5
    inputs = tmp_path / "p.json"
    inputs.write_text(json.dumps(inputs_data), encoding="utf-8")
    out_dir = tmp_path / "docs"
    ctx = run(inputs_path=inputs, out_dir=out_dir)
    assert ctx.suggestion.action == ActionSuggestion.STAND_ASIDE


def test_run_with_kill_switch_active(tmp_path: Path) -> None:
    inputs_data = _green_inputs()
    inputs_data["journal"]["kill_switch_active"] = True
    inputs = tmp_path / "p.json"
    inputs.write_text(json.dumps(inputs_data), encoding="utf-8")
    out_dir = tmp_path / "docs"
    ctx = run(inputs_path=inputs, out_dir=out_dir)
    assert ctx.suggestion.action == ActionSuggestion.KILL


# --------------------------------------------------------------------------- #
# _render_text
# --------------------------------------------------------------------------- #


def test_render_text_contains_all_sections(tmp_path: Path) -> None:
    inputs = tmp_path / "p.json"
    inputs.write_text(json.dumps(_green_inputs()), encoding="utf-8")
    out_dir = tmp_path / "docs"
    ctx = run(inputs_path=inputs, out_dir=out_dir)
    text = _render_text(ctx)
    assert "PRE-MARKET BRIEFING" in text
    assert "ACTION:" in text
    assert "REGIME" in text
    assert "MACRO" in text
    assert "EQUITY / RISK" in text
    assert "JOURNAL" in text
