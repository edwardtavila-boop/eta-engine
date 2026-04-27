"""Tests for scripts.weekly_review."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from eta_engine.scripts import weekly_review as mod

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fake_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    # v1 spec with per_bot
    v1 = {
        "spec_id": "APEX_PAPER_RESULTS_v1",
        "harness_run": {
            "per_bot": {
                "mnq": {"trades": 168, "expectancy_r": 0.473, "max_dd_pct": 6.83, "gate": "PASS"},
                "nq": {"trades": 140, "expectancy_r": 0.607, "max_dd_pct": 3.94, "gate": "PASS"},
                "crypto_seed": {"trades": 161, "expectancy_r": 0.149, "max_dd_pct": 4.93, "gate": "FAIL"},
            },
        },
        "promotion_path_proposed": {
            "tier_A_graduate_to_live_tiny": ["mnq", "nq"],
            "tier_B_hold_at_paper_gate": ["crypto_seed", "eth_perp", "sol_perp", "xrp_perp"],
        },
    }
    (docs / "firm_spec_paper_results_v1.json").write_text(json.dumps(v1))
    # Kill log
    (docs / "kill_log.json").write_text(json.dumps({"meta": {}, "entries": [{"id": 1}, {"id": 2}]}))
    return docs


def test_pick_latest_spec(fake_docs: Path):
    import os
    import time

    # Create v2 newer than v1 with explicit mtime bump (filesystem may batch mtimes)
    v1 = fake_docs / "firm_spec_paper_results_v1.json"
    v2 = fake_docs / "firm_spec_paper_results_v2.json"
    v2.write_text(json.dumps({"spec_id": "APEX_PAPER_RESULTS_v2"}))
    now = time.time()
    os.utime(v1, (now - 10, now - 10))
    os.utime(v2, (now, now))
    picked = mod._pick_latest_spec()
    assert picked.name == v2.name


def test_tier_bots_extract(fake_docs: Path):
    spec = json.loads((fake_docs / "firm_spec_paper_results_v1.json").read_text())
    assert mod._tier_bots(spec, "A") == ["mnq", "nq"]
    assert "eth_perp" in mod._tier_bots(spec, "B")
    assert mod._tier_bots(spec, "Z") == []


def test_metrics_from_spec_tier_a(fake_docs: Path):
    spec = json.loads((fake_docs / "firm_spec_paper_results_v1.json").read_text())
    trades, exp_r, dd = mod._metrics_from_spec(spec, "A")
    assert trades == 308
    # Blended: (168*.473 + 140*.607) / 308 ≈ 0.534
    assert 0.53 < exp_r < 0.54
    assert dd == 6.83


def test_metrics_from_spec_tier_b_falls_through_chain(fake_docs: Path):
    # Build v2 that points to v1 as parent
    v2 = {
        "spec_id": "APEX_PAPER_RESULTS_v2",
        "parent_spec": "APEX_PAPER_RESULTS_v1",
        "harness_run_v2c": {
            "aggregate_tier_b": {
                "total_trades": 687,
                "blended_expectancy_r": 0.271,
                "blended_max_dd_pct": 13.14,
            },
        },
    }
    trades, exp_r, dd = mod._metrics_from_spec(v2, "B")
    assert trades == 687
    assert abs(exp_r - 0.271) < 1e-9
    assert abs(dd - 13.14) < 1e-9


def test_kill_log_count(fake_docs: Path):
    assert mod._kill_log_count() == 2
    (fake_docs / "kill_log.json").unlink()
    assert mod._kill_log_count() == 0


def test_actions_from_verdict_covers_branches():
    a_go = mod._actions_from_verdict("GO", "A")
    b_go = mod._actions_from_verdict("GO", "B")
    assert any("live-tiny" in a.lower() for a in a_go)
    assert any("correlation" in a.lower() for a in b_go)
    # Tier A/B GO actions must differ
    assert a_go != b_go
    assert any("paper" in a.lower() for a in mod._actions_from_verdict("MODIFY", "A"))
    assert any("Kill" in a for a in mod._actions_from_verdict("KILL", "B"))
    assert "manual triage" in " ".join(mod._actions_from_verdict("WTF", "A"))


def test_write_emits_all_three_files(fake_docs: Path):
    entry = mod.ReviewEntry(
        generated_at_utc="2026-04-16T00:00:00+00:00",
        week_of="2026-W16",
        spec_id="APEX_PAPER_RESULTS_v2",
        spec_path="/tmp/spec.json",
        tier="A",
        bots_in_scope=["mnq", "nq"],
        trades=308,
        blended_expectancy_r=0.534,
        blended_dd_pct=6.83,
        firm_verdict="GO",
        quant_vote="GO",
        risk_vote="CONTINUE",
        redteam_vote="CONTINUE",
        macro_vote="CONTINUE",
        micro_vote="CONTINUE",
        pm_vote="GO",
        actions_required=["Proceed"],
        kill_log_entries_at_time=2,
    )
    log_p, latest_j, latest_t = mod._write(entry, fake_docs, raw_firm_blob="")
    assert log_p.exists() and latest_j.exists() and latest_t.exists()
    hist = json.loads(log_p.read_text())
    assert isinstance(hist, list) and len(hist) == 1
    assert hist[0]["spec_id"] == "APEX_PAPER_RESULTS_v2"

    # Append idempotency — second write grows the log
    mod._write(entry, fake_docs, raw_firm_blob="")
    hist2 = json.loads(log_p.read_text())
    assert len(hist2) == 2
