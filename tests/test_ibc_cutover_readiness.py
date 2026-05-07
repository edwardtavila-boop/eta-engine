from __future__ import annotations

import json


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


def test_main_writes_canonical_payload(tmp_path, monkeypatch, capsys) -> None:
    from eta_engine.scripts import ibc_cutover_readiness as mod

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
