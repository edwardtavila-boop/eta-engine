from __future__ import annotations

import json
from pathlib import Path

from eta_engine.scripts import supercharge_orchestrator as orchestrator


def _empty_phase1() -> dict:
    return {"source": "skipped", "n_attempted": 0, "n_fetched": 0, "failed": []}


def test_tier_filter_keeps_equity_index_symbols_for_rth_mnq() -> None:
    symbols = {("MNQ", "5m"), ("NQ", "5m"), ("CL", "5m"), ("GC", "5m")}

    filtered = orchestrator._tier_filter_symbols(symbols, "rth-mnq")

    assert filtered == {("MNQ", "5m"), ("NQ", "5m")}


def test_phase2_hourly_tier_skips_harness() -> None:
    result = orchestrator.phase2_elite_gate(tier="hourly", dry_run=True)

    assert result["tier"] == "hourly-skip-harness"
    assert result["n_bots"] == 0
    assert result["verdicts"] == []


def test_phase8_summary_dry_run_does_not_write_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "LOG_DIR", tmp_path)

    digest = orchestrator.phase8_summary(
        _empty_phase1(),
        {"days_window": 5, "n_bots": 0, "n_skipped_cached": 0, "verdicts": []},
        {"n_consulted": 0, "agreements": [], "dissents": []},
        {"n_arbitrated": 0},
        {"proposed_promote": [], "proposed_demote": []},
        run_id="dry",
        tier="sweep",
        dry_run=True,
    )

    assert digest["run_id"] == "dry"
    assert not (tmp_path / "supercharge_runs.jsonl").exists()


def test_phase8_summary_writes_log_when_not_dry_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "LOG_DIR", tmp_path)

    orchestrator.phase8_summary(
        _empty_phase1(),
        {"days_window": 5, "n_bots": 0, "n_skipped_cached": 0, "verdicts": []},
        {"n_consulted": 0, "agreements": [], "dissents": []},
        {"n_arbitrated": 0},
        {"proposed_promote": [], "proposed_demote": []},
        run_id="write",
        tier="sweep",
    )

    lines = (tmp_path / "supercharge_runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["run_id"] == "write"
