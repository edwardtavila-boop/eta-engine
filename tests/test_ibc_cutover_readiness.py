from __future__ import annotations

import json
from pathlib import Path

import pytest

from eta_engine.scripts import workspace_roots


def test_readiness_waits_for_credentials_when_runtime_is_installed(monkeypatch) -> None:
    from eta_engine.scripts import ibc_cutover_readiness as mod

    monkeypatch.setattr(
        mod,
        "_read_json",
        lambda path: (
            {"installed": True, "install_dir": "C:\\ibc\\3.23.0"}
            if path == mod._IBC_INSTALL_STATE_PATH
            else (
                {"launcher_mode": "direct"}
                if path == mod._IBG_REPAIR_STATE_PATH
                else (
                    {"status": "ready_to_launch_paper_live", "critical_ready": True}
                    if path == mod._PAPER_LIVE_STATE_PATH
                    else {"healthy": True}
                )
            )
        ),
    )
    monkeypatch.setattr(
        mod,
        "_collect_credential_sources",
        lambda: {
            "login_present": True,
            "password_present": False,
            "login_sources": ["json:ibkr_credentials"],
            "password_sources": [],
            "eta_env_path": "C:\\EvolutionaryTradingAlgo\\eta_engine\\.env",
            "ibkr_json_path": "C:\\EvolutionaryTradingAlgo\\eta_engine\\secrets\\ibkr_credentials.json",
            "ibc_private_config_path": "C:\\EvolutionaryTradingAlgo\\var\\eta_engine\\ibc\\private\\config.ini",
            "ibc_private_config_exists": False,
        },
    )

    payload = mod.build_readiness()

    assert payload["status"] == "staged_waiting_for_credentials"
    assert payload["direct_lane_ready"] is True
    assert payload["unattended_credential_ready"] is False
    assert "set_ibc_credentials.ps1 -PromptForPassword" in payload["operator_action"]


def test_readiness_prioritizes_credentials_before_direct_lane(monkeypatch) -> None:
    from eta_engine.scripts import ibc_cutover_readiness as mod

    monkeypatch.setattr(
        mod,
        "_read_json",
        lambda path: (
            {"installed": True, "install_dir": "C:\\ibc\\3.23.0"}
            if path == mod._IBC_INSTALL_STATE_PATH
            else (
                {"launcher_mode": "ibc"}
                if path == mod._IBG_REPAIR_STATE_PATH
                else (
                    {"status": "blocked", "critical_ready": False}
                    if path == mod._PAPER_LIVE_STATE_PATH
                    else {"healthy": False}
                )
            )
        ),
    )
    monkeypatch.setattr(
        mod,
        "_collect_credential_sources",
        lambda: {
            "login_present": True,
            "password_present": False,
            "login_sources": ["ibc_private_config:IbLoginId"],
            "password_sources": [],
            "eta_env_path": "C:\\EvolutionaryTradingAlgo\\eta_engine\\.env",
            "ibkr_json_path": "C:\\EvolutionaryTradingAlgo\\eta_engine\\secrets\\ibkr_credentials.json",
            "ibc_private_config_path": "C:\\EvolutionaryTradingAlgo\\var\\eta_engine\\ibc\\private\\config.ini",
            "ibc_private_config_exists": True,
        },
    )

    payload = mod.build_readiness()

    assert payload["status"] == "staged_waiting_for_credentials"
    assert payload["direct_lane_ready"] is False
    assert payload["unattended_credential_ready"] is False
    assert "set_ibc_credentials.ps1 -PromptForPassword" in payload["operator_action"]


def test_readiness_reports_ready_for_cutover_when_unattended_credentials_exist(monkeypatch) -> None:
    from eta_engine.scripts import ibc_cutover_readiness as mod

    monkeypatch.setattr(
        mod,
        "_read_json",
        lambda path: (
            {
                "installed": True,
                "install_dir": "C:\\ibc\\3.23.0",
                "current_install_dir": "C:\\ibc\\3.23.0",
                "start_ibc_path": "C:\\ibc\\3.23.0\\scripts\\StartIBC.bat",
            }
            if path == mod._IBC_INSTALL_STATE_PATH
            else (
                {"launcher_mode": "direct"}
                if path == mod._IBG_REPAIR_STATE_PATH
                else (
                    {"status": "ready_to_launch_paper_live", "critical_ready": True}
                    if path == mod._PAPER_LIVE_STATE_PATH
                    else {"healthy": True}
                )
            )
        ),
    )
    monkeypatch.setattr(
        mod,
        "_collect_credential_sources",
        lambda: {
            "login_present": True,
            "password_present": True,
            "login_sources": ["machine_env:ETA_IBC_LOGIN_ID"],
            "password_sources": ["machine_env:ETA_IBC_PASSWORD"],
            "eta_env_path": "C:\\EvolutionaryTradingAlgo\\eta_engine\\.env",
            "ibkr_json_path": "C:\\EvolutionaryTradingAlgo\\eta_engine\\secrets\\ibkr_credentials.json",
            "ibc_private_config_path": "C:\\EvolutionaryTradingAlgo\\var\\eta_engine\\ibc\\private\\config.ini",
            "ibc_private_config_exists": True,
        },
    )

    payload = mod.build_readiness()

    assert payload["status"] == "ready_for_ibc_cutover"
    assert payload["operator_action_required"] is False
    assert payload["unattended_credential_ready"] is True
    assert "repair_ibgateway_vps.ps1" in payload["operator_action"]


def test_readiness_reports_active_when_tasks_already_use_ibc(monkeypatch) -> None:
    from eta_engine.scripts import ibc_cutover_readiness as mod

    monkeypatch.setattr(
        mod,
        "_read_json",
        lambda path: (
            {"installed": True}
            if path == mod._IBC_INSTALL_STATE_PATH
            else (
                {"single_source": {"task_actions": {"ETA-IBGateway-RunNow": "powershell ... -UseIbc"}}}
                if path == mod._IBG_REPAIR_STATE_PATH
                else (
                    {"status": "ready_to_launch_paper_live", "critical_ready": True}
                    if path == mod._PAPER_LIVE_STATE_PATH
                    else {"healthy": True}
                )
            )
        ),
    )
    monkeypatch.setattr(
        mod,
        "_collect_credential_sources",
        lambda: {
            "login_present": True,
            "password_present": True,
            "login_sources": ["machine_env:ETA_IBC_LOGIN_ID"],
            "password_sources": ["machine_env:ETA_IBC_PASSWORD"],
            "eta_env_path": "C:\\EvolutionaryTradingAlgo\\eta_engine\\.env",
            "ibkr_json_path": "C:\\EvolutionaryTradingAlgo\\eta_engine\\secrets\\ibkr_credentials.json",
            "ibc_private_config_path": "C:\\EvolutionaryTradingAlgo\\var\\eta_engine\\ibc\\private\\config.ini",
            "ibc_private_config_exists": True,
        },
    )

    payload = mod.build_readiness()

    assert payload["status"] == "ibc_cutover_active"
    assert payload["launcher_mode"] == "ibc"
    assert payload["operator_action_required"] is False


def test_collect_credential_sources_ignores_placeholder_private_password(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from eta_engine.scripts import ibc_cutover_readiness as mod

    private_config = tmp_path / "private" / "config.ini"
    private_config.parent.mkdir(parents=True)
    private_config.write_text(
        "IbLoginId=paper_user\nIbPassword=<REAL_IBKR_PASSWORD>\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_ETA_ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(mod, "_IBKR_JSON_PATH", tmp_path / "ibkr_credentials.json")
    monkeypatch.setattr(mod, "_IBC_PRIVATE_CONFIG_PATH", private_config)
    monkeypatch.setattr(mod, "_IBC_PASSWORD_FILES", ())
    monkeypatch.setattr(mod, "_registry_sources", lambda name: [])

    sources = mod._collect_credential_sources()

    assert sources["login_present"] is True
    assert sources["password_present"] is False
    assert sources["login_sources"] == ["ibc_private_config:IbLoginId"]
    assert sources["password_sources"] == []


def test_main_writes_canonical_payload(tmp_path, monkeypatch, capsys) -> None:
    from eta_engine.scripts import ibc_cutover_readiness as mod

    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(
        mod,
        "build_readiness",
        lambda: {
            "schema_version": 1,
            "status": "staged_waiting_for_credentials",
            "operator_action_required": True,
            "operator_action": "seed password",
        },
    )

    out = tmp_path / "ibc_cutover_readiness.json"
    rc = mod.main(["--out", str(out), "--strict"])

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 2
    assert payload["status"] == "staged_waiting_for_credentials"
    assert json.loads(capsys.readouterr().out)["status"] == "staged_waiting_for_credentials"


def test_cli_rejects_output_path_outside_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from eta_engine.scripts import ibc_cutover_readiness as mod

    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "ibc_cutover_readiness.json"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        mod,
        "build_readiness",
        lambda: (_ for _ in ()).throw(AssertionError("readiness should not build")),
    )

    with pytest.raises(SystemExit) as exc:
        mod.main(["--out", str(outside_workspace)])

    assert exc.value.code == 2
