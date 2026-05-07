from __future__ import annotations

import json
import sys
from pathlib import Path


def test_secret_gate_warns_unless_strict() -> None:
    from eta_engine.scripts.project_kaizen_closeout import _classify_command_exit

    assert _classify_command_exit("secrets_validator", 1, strict_secrets=False) == "pass"
    assert _classify_command_exit("secrets_validator", 1, strict_secrets=True) == "fail"
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


def test_closeout_uses_wiring_preflight_for_submodule_gate(tmp_path: Path, monkeypatch) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    def fake_run_command(args, *, cwd, timeout_s):
        stdout = ""
        if "eta_engine.scripts.submodule_wiring_preflight" in args:
            stdout = json.dumps({"ready": True, "action": "safe_to_wire_gitlinks"})
        elif "submodule" in args:
            stdout = (
                " 15e701e12bdd09995847d279861b3c12b0ba06f2 eta_engine (main)\n"
                "-1c3a2ef93a2d25561a4ec3e022cdbe1176ce590a eta/legacy_child\n"
            )
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout=stdout,
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)

    report = closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=False,
        run_tests=False,
        strict_secrets=False,
    )

    submodule_gate = next(gate for gate in report["gates"] if gate["name"] == "submodule_status")
    assert submodule_gate["status"] == "pass"
    assert "eta_engine.scripts.submodule_wiring_preflight" in submodule_gate["extra"]["args"]


def test_live_closeout_allows_remote_supervisor_truth_for_local_health(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import project_kaizen_closeout as closeout

    commands: list[list[str]] = []

    def fake_run_command(args, *, cwd, timeout_s):
        commands.append(list(args))
        return closeout.CommandResult(
            args=list(args),
            cwd=str(cwd),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.01,
        )

    monkeypatch.setattr(closeout, "_run_command", fake_run_command)
    monkeypatch.setattr(
        closeout,
        "_live_endpoint_gate",
        lambda name, url, *, timeout_s: closeout._gate(name, "pass", "ok"),
    )

    closeout.run_closeout(
        root=tmp_path,
        output_dir=tmp_path / "state",
        python_exe=sys.executable,
        include_live=True,
        run_tests=False,
    )

    health_args = next(args for args in commands if "eta_engine.scripts.health_check" in args)
    assert "--allow-remote-supervisor-truth" in health_args
