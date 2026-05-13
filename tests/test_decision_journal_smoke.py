from __future__ import annotations

import json
from typing import TYPE_CHECKING

from eta_engine.scripts import decision_journal_smoke

if TYPE_CHECKING:
    from pathlib import Path


def test_append_decision_journal_smoke_writes_canonical_jsonl_shape(tmp_path: Path) -> None:
    journal_path = tmp_path / "var" / "eta_engine" / "state" / "decision_journal.jsonl"

    evidence = decision_journal_smoke.append_decision_journal_smoke(
        journal_path,
        source="pytest",
    )

    assert evidence["path"].replace("\\", "/").endswith("var/eta_engine/state/decision_journal.jsonl")
    assert evidence["bytes"] > 0
    lines = [line for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["actor"] == "OPERATOR"
    assert record["intent"] == "decision_journal_smoke"
    assert record["outcome"] == "NOTED"
    assert record["metadata"]["source"] == "pytest"
    assert record["metadata"]["status"] == "green"
    assert record["metadata"]["dry_run"] is True
    assert record["metadata"]["broker_network"] is False
    assert record["metadata"]["supabase_mirror"] is False


def test_decision_journal_smoke_main_prints_json_evidence(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    journal_path = tmp_path / "decision_journal.jsonl"

    rc = decision_journal_smoke.main(
        ["--journal-path", str(journal_path), "--source", "cli-test", "--json"],
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["metadata"]["source"] == "cli-test"
    assert journal_path.exists()
