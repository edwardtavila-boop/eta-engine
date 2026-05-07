from __future__ import annotations

import json
import sys
from pathlib import Path


def test_secret_gate_warns_unless_strict() -> None:
    from eta_engine.scripts.project_kaizen_closeout import _classify_command_exit

    assert _classify_command_exit("secrets_validator", 2, strict_secrets=False) == "warn"
    assert _classify_command_exit("secrets_validator", 2, strict_secrets=True) == "fail"


def test_closeout_writes_latest_report(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="ok",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    output_dir = tmp_path / "var" / "eta_engine" / "state"

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=output_dir,
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    latest = output_dir / "kaizen_closeout_latest.json"
    assert latest.exists()
    assert json.loads(latest.read_text(encoding="utf-8"))["status"] == report["status"]
    assert report["root"] == str(tmp_path)
    assert {gate["name"] for gate in report["gates"]} >= {
        "canonical_root",
        "eta_engine_diff_check",
        "secrets_validator",
        "health_check",
    }
