from __future__ import annotations

import json
from pathlib import Path

from eta_engine.scripts import paper_trade_preflight as mod


def test_check_venue_reports_missing_when_router_absent(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(mod, "VENUES_DIR", tmp_path / "missing_venues")

    ok, message = mod._check_venue()

    assert ok is False
    assert message == "no venue router; paper-sim only"


def test_check_venue_counts_router_files(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    venues_dir = tmp_path / "venues"
    (venues_dir / "ibkr").mkdir(parents=True)
    (venues_dir / "ibkr" / "router_ibkr.py").write_text("# router\n", encoding="utf-8")
    (venues_dir / "paper").mkdir(parents=True)
    (venues_dir / "paper" / "router_paper.py").write_text("# router\n", encoding="utf-8")
    monkeypatch.setattr(mod, "VENUES_DIR", venues_dir)

    ok, message = mod._check_venue()

    assert ok is True
    assert message == "venue router(s) found: 2"


def test_check_journal_touches_runtime_path(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    journal_path = tmp_path / "var" / "eta_engine" / "state" / "decision_journal.jsonl"
    monkeypatch.setattr(mod.workspace_roots, "ETA_RUNTIME_DECISION_JOURNAL_PATH", journal_path)

    ok, message = mod._check_journal()

    assert ok is True
    assert message == str(journal_path)
    assert journal_path.exists()


def test_main_json_prints_preflight_payload(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    rows = [
        mod.PreflightVerdict(
            bot_id="mnq_ready",
            strategy_id="mnq_ready_v1",
            overall="READY",
            checks={"data": True, "baseline": True},
            reasons=[],
        ),
        mod.PreflightVerdict(
            bot_id="eth_warn",
            strategy_id="eth_warn_v1",
            overall="WARN",
            checks={"data": True, "baseline": False},
            reasons=["baseline: no baseline entry"],
        ),
    ]
    monkeypatch.setattr(mod, "run_preflight", lambda bot_filter=None: rows)

    rc = mod.main(["--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["bot_id"] == "mnq_ready"
    assert payload[1]["reasons"] == ["baseline: no baseline entry"]


def test_main_table_prints_summary(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    rows = [
        mod.PreflightVerdict("mnq_ready", "mnq_ready_v1", "READY", {"data": True}, []),
        mod.PreflightVerdict("eth_block", "eth_block_v1", "BLOCK", {"data": False}, ["data: no dataset: ETH/1h"]),
    ]
    monkeypatch.setattr(mod, "run_preflight", lambda bot_filter=None: rows)

    rc = mod.main([])

    assert rc == 0
    output = capsys.readouterr().out
    assert "mnq_ready" in output
    assert "eth_block" in output
    assert "READY=1  WARN=0  BLOCK=1  / 2 total" in output
