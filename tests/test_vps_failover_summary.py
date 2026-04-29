from __future__ import annotations

import json

from eta_engine.scripts import vps_failover_drill, vps_failover_summary


def test_build_summary_extracts_blockers_and_next_commands(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    checks = [
        vps_failover_drill.CheckResult(
            name="secrets_present",
            severity="amber",
            summary=".env missing",
            details={"copy_command": "cp .env.example .env && chmod 600 .env"},
        ),
        vps_failover_drill.CheckResult(
            name="install_script_syntax",
            severity="amber",
            summary="bash unavailable",
            details={"vps_commands": ["cd ~/eta_engine && bash -n deploy/install_vps.sh"]},
        ),
        vps_failover_drill.CheckResult(
            name="idempotent_resume",
            severity="green",
            summary="covered",
        ),
    ]
    monkeypatch.setattr(vps_failover_drill, "collect_checks", lambda **_kwargs: checks)

    summary = vps_failover_summary.build_summary()

    assert summary["overall_severity"] == "amber"
    assert summary["exit_code"] == 2
    assert summary["counts"] == {"red": 0, "amber": 2, "green": 1, "skip": 0}
    assert [blocker["name"] for blocker in summary["blockers"]] == [
        "secrets_present",
        "install_script_syntax",
    ]
    assert summary["blockers"][0]["next_commands"] == [
        "cp .env.example .env && chmod 600 .env",
        "$EDITOR .env",
    ]
    assert summary["blockers"][1]["next_commands"] == [
        "cd ~/eta_engine && bash -n deploy/install_vps.sh",
    ]


def test_summary_adds_smoke_commands_for_missing_canonical_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    checks = [
        vps_failover_drill.CheckResult(
            name="state_files_present",
            severity="amber",
            summary="recommended state files missing",
            details={
                "missing": [
                    "var/eta_engine/state/decision_journal.jsonl",
                    "logs/eta_engine/runtime_log.jsonl",
                    "var/eta_engine/state/drift_watchdog.jsonl",
                ]
            },
        )
    ]
    monkeypatch.setattr(vps_failover_drill, "collect_checks", lambda **_kwargs: checks)

    summary = vps_failover_summary.build_summary()

    commands = summary["blockers"][0]["next_commands"]
    assert "python -m eta_engine.scripts.decision_journal_smoke --json" in commands
    assert "python -m eta_engine.scripts.runtime_log_smoke --json" in commands
    assert "python -m eta_engine.scripts.drift_watchdog_smoke --json" in commands


def test_summary_json_main_prints_machine_readable_payload(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    checks = [
        vps_failover_drill.CheckResult(name="deploy_files_present", severity="green", summary="ok")
    ]
    monkeypatch.setattr(vps_failover_drill, "collect_checks", lambda **_kwargs: checks)

    rc = vps_failover_summary.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["overall_severity"] == "green"
    assert payload["blockers"] == []
