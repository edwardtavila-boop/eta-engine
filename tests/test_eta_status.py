"""Tests for the operator-facing eta_status summary."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from eta_engine.scripts import eta_status as mod  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _patch_status_paths(monkeypatch, tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    monkeypatch.setattr(mod, "HEARTBEAT_PATH", state_dir / "heartbeat.json")
    monkeypatch.setattr(mod, "LEADERBOARD", state_dir / "leaderboard.json")
    monkeypatch.setattr(mod, "LAUNCH_READINESS", state_dir / "launch_readiness.json")
    monkeypatch.setattr(mod, "KAIZEN_LATEST", state_dir / "kaizen.json")
    monkeypatch.setattr(mod, "EVENTS_LOG", state_dir / "events.jsonl")
    monkeypatch.setattr(mod, "QUANTUM_DIR", state_dir / "quantum")
    return state_dir


def test_gather_marks_stale_launch_readiness_and_keeps_gate_details(monkeypatch, tmp_path: Path) -> None:
    state_dir = _patch_status_paths(monkeypatch, tmp_path)
    stale_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

    _write_json(state_dir / "heartbeat.json", {"ts": stale_ts, "tick_count": 9, "n_bots": 15, "mode": "paper_sim"})
    _write_json(state_dir / "leaderboard.json", {"n_diamonds": 14, "n_prop_ready": 0, "prop_ready_bots": []})
    _write_json(state_dir / "kaizen.json", {})
    _write_json(
        state_dir / "launch_readiness.json",
        {
            "ts": stale_ts,
            "overall_verdict": "NO_GO",
            "summary": "NO_GO - 1 hard gate failing",
            "launch_date": "2026-05-18",
            "days_until_launch": 4,
            "gates": [
                {
                    "name": "R1_PROP_READY_DESIGNATED",
                    "status": "NO_GO",
                    "rationale": "only 0 PROP_READY bot(s); need >= 2 before going live",
                    "detail": {"n": 0},
                },
                {
                    "name": "R7_LEDGER_FRESH",
                    "status": "HOLD",
                    "rationale": "ledger stale",
                    "detail": {"age_hours": 20.8},
                },
            ],
        },
    )

    state = mod.gather()
    launch = state["launch_readiness"]

    assert launch["stale"] is True
    assert launch["age_seconds"] >= 60 * 60
    assert launch["failing_gates"] == ["R1_PROP_READY_DESIGNATED"]
    assert launch["warning_gates"] == ["R7_LEDGER_FRESH"]
    assert launch["failing_gate_details"][0]["detail"]["n"] == 0
    assert "PROP_READY" in launch["failing_gate_details"][0]["rationale"]


def test_render_text_prints_readiness_freshness_and_gate_rationales() -> None:
    state = {
        "ts": "2026-05-14T00:00:00+00:00",
        "supervisor": {"tick_count": 1, "n_bots": 15, "mode": "paper_sim", "ts": "2026-05-14T00:00:00+00:00"},
        "fm_cache": {"hits": 0, "misses": 0, "hit_rate_pct": 0.0, "size": 0, "ttl_seconds": 0},
        "fm_breaker": {"spent_today_usd": 0.0, "cap_usd": 1.0, "headroom_pct": 100.0, "tripped": False},
        "diamond_leaderboard": {"n_diamonds": 14, "n_prop_ready": 0, "prop_ready_bots": []},
        "launch_readiness": {
            "verdict": "NO_GO",
            "days_until_launch": 4,
            "launch_date": "2026-05-18",
            "age_seconds": 7200.0,
            "stale": True,
            "ts": "2026-05-13T22:00:00+00:00",
            "failing_gates": ["R1_PROP_READY_DESIGNATED"],
            "warning_gates": ["R7_LEDGER_FRESH"],
            "failing_gate_details": [
                {
                    "name": "R1_PROP_READY_DESIGNATED",
                    "rationale": "only 0 PROP_READY bot(s); need >= 2 before going live",
                },
            ],
            "warning_gate_details": [{"name": "R7_LEDGER_FRESH", "rationale": "ledger stale"}],
            "summary": "NO_GO - 1 hard gate failing",
        },
        "kaizen": {"bootstraps": None, "n_bots": None, "applied_count": None, "ts": None, "action_counts": {}},
        "quantum_6h": {
            "rebalanced": None,
            "skipped": None,
            "total_cost_usd": 0.0,
            "ts": None,
            "instruments_with_signals": [],
            "instruments_with_hedges": [],
        },
        "recent_events": [],
    }

    text = mod.render_text(state)

    assert "freshness    : STALE" in text
    assert "R1_PROP_READY_DESIGNATED: only 0 PROP_READY bot(s)" in text
    assert "R7_LEDGER_FRESH: ledger stale" in text
