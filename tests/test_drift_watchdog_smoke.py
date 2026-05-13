from __future__ import annotations

import json
from typing import TYPE_CHECKING

from eta_engine.scripts import drift_watchdog_smoke

if TYPE_CHECKING:
    from pathlib import Path


def test_append_drift_watchdog_smoke_writes_canonical_jsonl_shape(tmp_path: Path) -> None:
    log_path = tmp_path / "var" / "eta_engine" / "state" / "drift_watchdog.jsonl"

    evidence = drift_watchdog_smoke.append_drift_watchdog_smoke(log_path, source="pytest")

    assert evidence["path"].replace("\\", "/").endswith("var/eta_engine/state/drift_watchdog.jsonl")
    assert evidence["bytes"] > 0
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "drift_watchdog_smoke"
    assert record["source"] == "pytest"
    assert record["severity"] == "green"
    assert record["status"] == "green"
    assert record["dry_run"] is True
    assert record["strategy_replay"] is False
    assert record["broker_network"] is False


def test_drift_watchdog_smoke_main_prints_json_evidence(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    log_path = tmp_path / "drift_watchdog.jsonl"

    rc = drift_watchdog_smoke.main(["--log-path", str(log_path), "--source", "cli-test", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["source"] == "cli-test"
    assert log_path.exists()
