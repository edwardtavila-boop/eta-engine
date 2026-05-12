from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

from eta_engine.feeds import vps_failover_drill as feeds_vps_failover_drill
from eta_engine.scripts import (
    _backup_state,
    _kill_switch_drift,
    _repo_health,
    _trade_journal_reconcile,
    vps_failover_drill,
    workspace_roots,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_default_alerts_log_prefers_canonical_runtime_path(monkeypatch, tmp_path: Path) -> None:
    canonical = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    legacy = tmp_path / "eta_engine" / "docs" / "alerts_log.jsonl"
    canonical.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    canonical.write_text('{"event":"runtime_start"}\n', encoding="utf-8")
    legacy.write_text('{"event":"legacy"}\n', encoding="utf-8")

    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_ALERTS_LOG_PATH", canonical)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_ALERTS_LOG_PATH", legacy)

    assert workspace_roots.default_alerts_log_path() == canonical


def test_default_alerts_log_falls_back_to_legacy_snapshot(monkeypatch, tmp_path: Path) -> None:
    canonical = tmp_path / "logs" / "eta_engine" / "missing.jsonl"
    legacy = tmp_path / "eta_engine" / "docs" / "alerts_log.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"event":"legacy"}\n', encoding="utf-8")

    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_ALERTS_LOG_PATH", canonical)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_ALERTS_LOG_PATH", legacy)

    assert workspace_roots.default_alerts_log_path() == legacy


def test_alert_readers_default_to_canonical_runtime_log() -> None:
    assert _kill_switch_drift.DEFAULT_LOG == workspace_roots.ETA_RUNTIME_ALERTS_LOG_PATH
    assert _trade_journal_reconcile.DEFAULT_ALERTS == workspace_roots.ETA_RUNTIME_ALERTS_LOG_PATH


def test_trade_journal_reconcile_runs_as_script_from_child_root(tmp_path: Path) -> None:
    alerts = tmp_path / "alerts_log.jsonl"
    btc = tmp_path / "btc_live_decisions.jsonl"
    alerts.write_text(
        json.dumps(
            {
                "event": "runtime_start",
                "ts": 1_800_000_000.0,
                "payload": {"live": True, "synthetic": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    btc.write_text("", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_trade_journal_reconcile.ROOT / "scripts" / "_trade_journal_reconcile.py"),
            "--alerts",
            str(alerts),
            "--btc",
            str(btc),
            "--hours",
            "24",
            "--now-utc",
            "1800003600",
        ],
        cwd=_trade_journal_reconcile.ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "orphaned-runtime" in result.stdout


def test_default_runtime_log_falls_back_to_legacy_snapshot(monkeypatch, tmp_path: Path) -> None:
    canonical = tmp_path / "logs" / "eta_engine" / "missing_runtime.jsonl"
    legacy = tmp_path / "eta_engine" / "docs" / "runtime_log.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"kind":"tick"}\n', encoding="utf-8")

    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_LOG_PATH", canonical)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_RUNTIME_LOG_PATH", legacy)

    assert workspace_roots.default_runtime_log_path() == legacy


def test_default_drift_watchdog_log_falls_back_to_legacy_snapshot(monkeypatch, tmp_path: Path) -> None:
    canonical = tmp_path / "var" / "eta_engine" / "state" / "missing_drift.jsonl"
    legacy = tmp_path / "eta_engine" / "docs" / "drift_watchdog.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"severity":"green"}\n', encoding="utf-8")

    monkeypatch.setattr(workspace_roots, "ETA_DRIFT_WATCHDOG_LOG_PATH", canonical)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_DRIFT_WATCHDOG_LOG_PATH", legacy)

    assert workspace_roots.default_drift_watchdog_log_path() == legacy


def test_backup_state_tracks_resolved_alert_log_first(monkeypatch, tmp_path: Path) -> None:
    canonical = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    canonical.parent.mkdir(parents=True)
    canonical.write_text('{"event":"runtime_start"}\n', encoding="utf-8")

    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_ALERTS_LOG_PATH", canonical)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_ALERTS_LOG_PATH", tmp_path / "legacy.jsonl")

    assert _backup_state.critical_files()[0] == canonical


def test_repo_health_tracks_resolved_runtime_logs_first(monkeypatch, tmp_path: Path) -> None:
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    runtime = tmp_path / "logs" / "eta_engine" / "runtime_log.jsonl"
    alerts.parent.mkdir(parents=True)
    alerts.write_text('{"event":"runtime_start"}\n', encoding="utf-8")
    runtime.write_text('{"kind":"tick"}\n', encoding="utf-8")

    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_ALERTS_LOG_PATH", alerts)
    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_LOG_PATH", runtime)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_ALERTS_LOG_PATH", tmp_path / "legacy_alerts.jsonl")
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_RUNTIME_LOG_PATH", tmp_path / "legacy_runtime.jsonl")

    assert _repo_health.log_files()[:2] == [alerts, runtime]


def test_vps_failover_tracks_workspace_runtime_logs(monkeypatch, tmp_path: Path) -> None:
    alerts = tmp_path / "logs" / "eta_engine" / "alerts_log.jsonl"
    runtime = tmp_path / "logs" / "eta_engine" / "runtime_log.jsonl"
    drift = tmp_path / "var" / "eta_engine" / "state" / "drift_watchdog.jsonl"
    alerts.parent.mkdir(parents=True)
    drift.parent.mkdir(parents=True)
    alerts.write_text('{"event":"runtime_start"}\n', encoding="utf-8")
    runtime.write_text('{"kind":"tick"}\n', encoding="utf-8")
    drift.write_text('{"severity":"green"}\n', encoding="utf-8")

    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_ALERTS_LOG_PATH", alerts)
    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_LOG_PATH", runtime)
    monkeypatch.setattr(workspace_roots, "ETA_DRIFT_WATCHDOG_LOG_PATH", drift)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_ALERTS_LOG_PATH", tmp_path / "legacy_alerts.jsonl")
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_RUNTIME_LOG_PATH", tmp_path / "legacy_runtime.jsonl")

    _, recommended = vps_failover_drill._state_file_paths()
    recommended_paths = [path for _, path in recommended]
    assert drift in recommended_paths
    assert alerts in recommended_paths
    assert runtime in recommended_paths


def test_vps_failover_tracks_workspace_decision_journal(monkeypatch, tmp_path: Path) -> None:
    journal = tmp_path / "var" / "eta_engine" / "state" / "decision_journal.jsonl"
    legacy_child_journal = vps_failover_drill.ROOT / "var" / "eta_engine" / "state" / "decision_journal.jsonl"
    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_DECISION_JOURNAL_PATH", journal)

    required, _ = vps_failover_drill._state_file_paths()
    required_paths = [path for _, path in required]

    assert journal in required_paths
    assert legacy_child_journal not in required_paths


def test_vps_failover_requires_canonical_runtime_log_not_legacy_fallback(monkeypatch, tmp_path: Path) -> None:
    canonical = tmp_path / "logs" / "eta_engine" / "runtime_log.jsonl"
    legacy = tmp_path / "eta_engine" / "docs" / "runtime_log.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"kind":"legacy"}\n', encoding="utf-8")

    monkeypatch.setattr(workspace_roots, "ETA_RUNTIME_LOG_PATH", canonical)
    monkeypatch.setattr(workspace_roots, "ETA_LEGACY_DOCS_RUNTIME_LOG_PATH", legacy)

    _, recommended = vps_failover_drill._state_file_paths()
    recommended_paths = [path for _, path in recommended]
    assert canonical in recommended_paths
    assert legacy not in recommended_paths


def test_vps_failover_archives_workspace_paths_relative_to_workspace() -> None:
    workspace_log = workspace_roots.WORKSPACE_ROOT / "logs" / "eta_engine" / "alerts_log.jsonl"
    assert vps_failover_drill._archive_name(workspace_log) == "logs/eta_engine/alerts_log.jsonl"


def test_vps_failover_missing_env_reports_template_and_active_brokers(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.example").write_text("IBKR_ACCOUNT_ID=\n", encoding="utf-8")
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)

    result = vps_failover_drill._check_secrets_present()

    assert result.severity == "amber"
    assert result.details["template"].replace("\\", "/").endswith(".env.example")
    assert result.details["template_exists"] is True
    assert result.details["copy_commands"][0].startswith(
        "python -m eta_engine.scripts.operator_env_bootstrap --create"
    )
    assert result.details["copy_commands"][1].startswith("Copy-Item")
    assert result.details["copy_commands"][-2] == "cp .env.example .env && chmod 600 .env"
    assert result.details["active_brokers"] == ["IBKR", "Tastytrade"]
    assert result.details["dormant_brokers"] == ["Tradovate"]
    assert result.details["required_groups"]["ibkr_primary"] == ["IBKR_VENUE_TYPE=paper"]
    assert "IBKR_ACCOUNT_ID" in result.details["recommended_groups"]["ibkr_client_portal_sidecars"]
    assert "TASTY_SESSION_TOKEN" in result.details["recommended_groups"]["tastytrade_fallback"]


def test_vps_failover_existing_env_with_empty_required_keys_stays_amber(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.example").write_text("ETA_MODE=\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ETA_MODE=",
                "ANTHROPIC_API_KEY=",
                "JARVIS_HOURLY_USD_BUDGET=5",
                "JARVIS_DAILY_USD_BUDGET=25",
                "IBKR_VENUE_TYPE=paper",
                "IBKR_CP_BASE_URL=",
                "IBKR_ACCOUNT_ID=DU123",
                "IBKR_CONID_MNQ1=",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)

    result = vps_failover_drill._check_secrets_present()

    assert result.severity == "amber"
    assert "runtime_mode" in result.details["required_missing"]
    assert "jarvis_budget" in result.details["required_missing"]
    assert "ibkr_primary" not in result.details["required_missing"]
    assert "ibkr_client_portal_sidecars" in result.details["recommended_missing"]
    assert result.details["values_emitted"] is False
    assert "DU123" not in json.dumps(result.details)


def test_vps_failover_inline_comments_do_not_count_as_populated_env_values(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "EMPTY_WITH_COMMENT=      # fill me",
                "QUOTED_EMPTY=''",
                "POPULATED=value # operator note",
            ]
        ),
        encoding="utf-8",
    )

    state = vps_failover_drill._env_key_state(vps_failover_drill._read_env_values(env_path))

    assert state["EMPTY_WITH_COMMENT"] is False
    assert state["QUOTED_EMPTY"] is False
    assert state["POPULATED"] is True


def test_vps_failover_required_env_keys_populated_without_tastytrade_is_green(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.example").write_text("ETA_MODE=\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ETA_MODE=PAPER",
                "ANTHROPIC_API_KEY=placeholder-secret",
                "JARVIS_HOURLY_USD_BUDGET=5",
                "JARVIS_DAILY_USD_BUDGET=25",
                "IBKR_VENUE_TYPE=paper",
                "IBKR_CP_BASE_URL=https://127.0.0.1:5000/v1/api",
                "IBKR_ACCOUNT_ID=DU123",
                "IBKR_CONID_MNQ1=123456",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)

    result = vps_failover_drill._check_secrets_present()

    assert result.severity == "green"
    assert result.details["required_missing"] == {}
    assert "tastytrade_fallback" in result.details["recommended_missing"]
    assert result.details["values_emitted"] is False
    assert "placeholder-secret" not in json.dumps(result.details)
    assert "DU123" not in json.dumps(result.details)


def test_vps_failover_env_template_completeness_passes_for_full_template(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.example").write_text(
        "\n".join(f"{token}=" for token in vps_failover_drill._ENV_TEMPLATE_REQUIRED_TOKENS),
        encoding="utf-8",
    )
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)

    result = vps_failover_drill._check_env_template_complete()

    assert result.severity == "green"


def test_vps_failover_env_template_completeness_fails_when_key_missing(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".env.example").write_text("ETA_MODE=PAPER\n", encoding="utf-8")
    monkeypatch.setattr(vps_failover_drill, "ROOT", tmp_path)

    result = vps_failover_drill._check_env_template_complete()

    assert result.severity == "red"
    assert "ANTHROPIC_API_KEY" in result.details["missing"]


def test_vps_failover_no_bash_reports_vps_validation_commands(monkeypatch) -> None:
    monkeypatch.setattr(vps_failover_drill, "_bash_candidates", lambda: [])

    result = vps_failover_drill._check_install_script_syntax()

    assert result.severity == "amber"
    assert result.details["reason"] == "bash_not_on_path"
    assert "bash -n deploy/install_vps.sh" in result.details["vps_commands"][0]
    assert "vps_failover_drill --no-backup-test --json" in result.details["vps_commands"][1]


def test_vps_failover_uses_repo_relative_install_script_for_git_bash(monkeypatch) -> None:
    modules = (vps_failover_drill, feeds_vps_failover_drill)

    for module in modules:
        calls = []
        monkeypatch.setattr(
            module,
            "_bash_candidates",
            lambda: ["C:/Program Files/Git/bin/bash.exe"],
        )

        def fake_run(args, _calls=calls, **kwargs):
            _calls.append((args, kwargs))
            script_arg = args[-1]
            if ":" in script_arg or "\\" in script_arg:
                return SimpleNamespace(
                    returncode=127,
                    stdout="",
                    stderr=f"/bin/bash: {script_arg}: No such file or directory",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(module.subprocess, "run", fake_run)

        result = module._check_install_script_syntax()

        assert result.severity == "green"
        assert calls[-1][0] == [
            "C:/Program Files/Git/bin/bash.exe",
            "-n",
            "deploy/install_vps.sh",
        ]
        assert calls[-1][1]["cwd"] == module.ROOT


def test_vps_failover_wsl_launcher_gap_is_amber(monkeypatch) -> None:
    monkeypatch.setattr(vps_failover_drill, "_bash_candidates", lambda: ["bash"])

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="Windows Subsystem for Linux has no installed distributions.",
            stderr="",
        )

    monkeypatch.setattr(vps_failover_drill.subprocess, "run", fake_run)
    result = vps_failover_drill._check_install_script_syntax()

    assert result.severity == "amber"
    assert "none could run scripts locally" in result.summary
    assert result.details["reason"] == "local_bash_launcher_unavailable"
    assert "bash -n deploy/install_vps.sh" in result.details["vps_commands"][0]


def test_vps_failover_falls_back_to_git_bash_after_wsl_launcher_gap(monkeypatch) -> None:
    monkeypatch.setattr(
        vps_failover_drill,
        "_bash_candidates",
        lambda: ["C:/Windows/System32/bash.exe", "C:/Program Files/Git/bin/bash.exe"],
    )

    def fake_run(args, **_kwargs):
        if args[0].endswith("System32/bash.exe"):
            return SimpleNamespace(
                returncode=1,
                stdout="Windows Subsystem for Linux has no installed distributions.",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vps_failover_drill.subprocess, "run", fake_run)

    result = vps_failover_drill._check_install_script_syntax()

    assert result.severity == "green"
    assert result.details["local_shell_path"].endswith("Git/bin/bash.exe")
    assert result.details["launcher_failures"][0]["reason"] == "local_bash_launcher_unavailable"


def test_vps_failover_real_bash_syntax_error_is_red(monkeypatch) -> None:
    monkeypatch.setattr(vps_failover_drill, "_bash_candidates", lambda: ["bash"])

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="deploy/install_vps.sh: line 12: syntax error near unexpected token",
        )

    monkeypatch.setattr(vps_failover_drill.subprocess, "run", fake_run)
    result = vps_failover_drill._check_install_script_syntax()

    assert result.severity == "red"
    assert "syntax error" in result.summary


def test_vps_failover_idempotent_resume_uses_live_order_evidence(monkeypatch, tmp_path: Path) -> None:
    router = tmp_path / "live_supervisor.py"
    preflight = tmp_path / "live_tiny_preflight_dryrun.py"
    router.write_text(
        "hashlib.sha256\n"
        "def _ensure_client_order_id(): pass\n"
        "client_order_id = 'coid'\n"
        "idempotent_order_id = True\n",
        encoding="utf-8",
    )
    preflight.write_text(
        "def _gate_idempotent_order_id(): pass\n"
        "JarvisAwareRouter._ensure_client_order_id\n"
        "client_order_id\n"
        "same coid\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        vps_failover_drill,
        "_IDEMPOTENCY_EVIDENCE_FILES",
        [
            (
                "deterministic_router",
                router,
                ("_ensure_client_order_id", "client_order_id", "idempotent_order_id", "hashlib.sha256"),
            ),
            (
                "required_preflight_gate",
                preflight,
                ("_gate_idempotent_order_id", "JarvisAwareRouter._ensure_client_order_id", "same coid"),
            ),
        ],
    )

    result = vps_failover_drill._check_idempotent_resume()

    assert result.severity == "green"
    assert "live deterministic order-id router" in result.summary
    assert [item["label"] for item in result.details["evidence"]] == [
        "deterministic_router",
        "required_preflight_gate",
    ]


def test_vps_failover_idempotent_resume_stays_amber_when_evidence_is_incomplete(
    monkeypatch, tmp_path: Path
) -> None:
    router = tmp_path / "live_supervisor.py"
    router.write_text("client_order_id = 'coid'\n", encoding="utf-8")
    missing_preflight = tmp_path / "live_tiny_preflight_dryrun.py"
    monkeypatch.setattr(
        vps_failover_drill,
        "_IDEMPOTENCY_EVIDENCE_FILES",
        [
            ("deterministic_router", router, ("_ensure_client_order_id", "client_order_id")),
            ("required_preflight_gate", missing_preflight, ("_gate_idempotent_order_id",)),
        ],
    )

    result = vps_failover_drill._check_idempotent_resume()

    assert result.severity == "amber"
    assert "evidence incomplete" in result.summary
    assert len(result.details["missing"]) == 2
