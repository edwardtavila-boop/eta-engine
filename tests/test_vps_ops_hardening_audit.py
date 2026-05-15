from __future__ import annotations

from eta_engine.scripts import vps_ops_hardening_audit as audit


def _running_services() -> dict[str, dict[str, object]]:
    return {name: {"name": name, "status": "Running", "start_type": "Automatic"} for name in audit.CRITICAL_SERVICES}


def _listening_ports() -> dict[int, dict[str, object]]:
    return {
        8000: {"port": 8000, "listening": True, "owners": ["dashboard_api"]},
        8421: {"port": 8421, "listening": True, "owners": ["reverse_proxy_bridge"]},
        8422: {"port": 8422, "listening": True, "owners": ["FmStatusServer"]},
        4002: {"port": 4002, "listening": True, "owners": ["IbcGateway"]},
    }


def _healthy_endpoints() -> dict[str, dict[str, object]]:
    return {
        "local_dashboard_api_diagnostics": {"ok": True, "status_code": 200},
        "local_dashboard_proxy_diagnostics": {"ok": True, "status_code": 200},
        "local_fm_status": {"ok": True, "status_code": 200},
        "public_ops_bot_fleet": {"ok": True, "status_code": 200},
    }


def _stale_dashboard_schema_endpoints() -> dict[str, dict[str, object]]:
    endpoints = _healthy_endpoints()
    stale_payload = {
        "vps_ops_hardening": {
            "status": "YELLOW_SAFETY_BLOCKED",
            "summary": {"runtime_ready": True},
        },
        "checks": {"vps_ops_hardening_contract": True},
    }
    endpoints["local_dashboard_api_diagnostics"] = {
        "ok": True,
        "status_code": 200,
        "payload": stale_payload,
    }
    endpoints["local_dashboard_proxy_diagnostics"] = {
        "ok": True,
        "status_code": 200,
        "payload": stale_payload,
    }
    return endpoints


def _healthy_tasks() -> dict[str, dict[str, object]]:
    return {
        name: {"task_name": name, "state": "Ready", "last_task_result": 0}
        for name in audit.DASHBOARD_DURABLE_TASKS
        + audit.PAPER_LIVE_DURABLE_TASKS
        + audit.DATA_PIPELINE_TASKS
        + audit.IBGATEWAY_TASKS
    }


def _blocked_bracket_gate() -> dict[str, object]:
    return {
        "summary": {
            "status": "BLOCKED_UNBRACKETED_EXPOSURE",
            "ready_for_prop_dry_run": False,
            "missing_bracket_count": 2,
            "missing_bracket_symbols": ["MNQM6", "NQM6"],
        }
    }


def _ready_bracket_gate() -> dict[str, object]:
    return {
        "summary": {
            "status": "PASS",
            "ready_for_prop_dry_run": True,
            "missing_bracket_count": 0,
            "missing_bracket_symbols": [],
        }
    }


def _blocked_promotion_gate() -> dict[str, object]:
    return {
        "summary": {
            "status": "BLOCKED_PAPER_SOAK",
            "ready_for_live": False,
        }
    }


def test_runtime_ok_but_trading_gates_blocked_is_yellow_not_red() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_blocked_bracket_gate(),
        promotion_audit=_blocked_promotion_gate(),
        service_config={"fm_status_server": {"matches_expected": True}},
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["promotion_allowed"] is False
    assert report["summary"]["order_action_allowed"] is False
    assert report["safety_gates"]["broker_brackets"]["missing_bracket_symbols"] == [
        "MNQM6",
        "NQM6",
    ]
    assert any("MNQM6, NQM6" in action for action in report["next_actions"])


def test_promotion_blocker_points_to_runner_up_when_primary_is_retired() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_ready_bracket_gate(),
        promotion_audit={
            "summary": "BLOCKED_KAIZEN_RETIRED",
            "ready_for_prop_dry_run_review": False,
            "next_runner_candidate": {
                "bot_id": "volume_profile_nq",
                "symbol": "NQ1",
                "broker_close_evidence": {"closed_trade_count": 0},
                "supervisor_watch_evidence": {"verdict": "WATCHING_NO_SIGNAL_YET"},
                "shadow_signal_evidence": {"signal_count": 3},
            },
            "required_evidence": [
                "evaluate runner-up candidate volume_profile_nq in paper soak; keep can_live_trade=false",
            ],
        },
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["safety_gates"]["promotion"]["status"] == "BLOCKED_KAIZEN_RETIRED"
    assert any("shadow signals into paper-close outcomes" in action for action in report["next_actions"])


def test_paper_live_gate_ready_when_only_prop_promotion_is_blocked() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_ready_bracket_gate(),
        promotion_audit=_blocked_promotion_gate(),
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["paper_live_gate_ready"] is True
    assert report["summary"]["paper_live_status"] == "READY_FOR_PAPER_SOAK"
    assert report["summary"]["prop_promotion_gate_ready"] is False
    assert report["summary"]["live_promotion_blocked"] is True
    assert report["summary"]["trading_gate_ready"] is False
    assert report["summary"]["promotion_allowed"] is False
    assert report["summary"]["order_action_allowed"] is False
    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"


def test_task_first_runtime_does_not_require_legacy_firm_services() -> None:
    services = {
        "FmStatusServer": {"name": "FmStatusServer", "status": "Running", "start_type": "Automatic"},
        "FirmCommandCenter": {"name": "FirmCommandCenter", "status": "Missing", "start_type": None},
        "FirmCommandCenterTunnel": {"name": "FirmCommandCenterTunnel", "status": "Missing", "start_type": None},
        "FirmCore": {"name": "FirmCore", "status": "Missing", "start_type": None},
        "FirmWatchdog": {"name": "FirmWatchdog", "status": "Missing", "start_type": None},
        "ETAJarvisSupervisor": {"name": "ETAJarvisSupervisor", "status": "Missing", "start_type": None},
    }

    report = audit.build_report(
        services=services,
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_blocked_bracket_gate(),
        promotion_audit=_blocked_promotion_gate(),
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert "FirmCore" in report["runtime"]["services"]["legacy_compatibility"]
    assert not any("FirmCore" in action for action in report["next_actions"])


def test_missing_critical_service_or_port_is_red_runtime_degraded() -> None:
    services = _running_services()
    services["FmStatusServer"] = {
        "name": "FmStatusServer",
        "status": "Stopped",
        "start_type": "Automatic",
    }
    ports = _listening_ports()
    ports[8422] = {"port": 8422, "listening": False, "owners": []}

    report = audit.build_report(
        services=services,
        ports=ports,
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_blocked_bracket_gate(),
        promotion_audit=_blocked_promotion_gate(),
        service_config={"fm_status_server": {"matches_expected": True}},
    )

    assert report["summary"]["status"] == "RED_RUNTIME_DEGRADED"
    assert report["summary"]["runtime_ready"] is False
    assert "FmStatusServer" in report["runtime"]["services"]["down"]
    assert 8422 in report["runtime"]["ports"]["missing"]


def test_missing_paper_live_durable_task_is_red_runtime_degraded() -> None:
    tasks = _healthy_tasks()
    tasks["ETA-TWS-Watchdog"] = {"task_name": "ETA-TWS-Watchdog", "state": "Missing", "last_task_result": None}

    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_blocked_bracket_gate(),
        promotion_audit=_blocked_promotion_gate(),
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=tasks,
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "RED_RUNTIME_DEGRADED"
    assert report["summary"]["runtime_ready"] is False
    assert report["runtime"]["tasks"]["missing_paper_live_durable"] == ["ETA-TWS-Watchdog"]
    assert any("paper-live scheduled task lane" in action for action in report["next_actions"])


def test_stale_watchdog_restart_hook_to_disabled_paperlive_is_runtime_risk() -> None:
    tasks = _healthy_tasks()
    tasks["ETA-Watchdog-Restart"] = {
        "task_name": "ETA-Watchdog-Restart",
        "state": "Ready",
        "last_task_result": 1,
        "actions": "cmd.exe /c schtasks /run /tn ETA-PaperLive-Supervisor",
    }

    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_blocked_bracket_gate(),
        promotion_audit=_blocked_promotion_gate(),
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=tasks,
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "RED_RUNTIME_DEGRADED"
    assert report["summary"]["runtime_ready"] is False
    assert report["runtime"]["tasks"]["stale_supervisor_restart_hooks"] == ["ETA-Watchdog-Restart"]
    assert any("ETA-Jarvis-Strategy-Supervisor" in action for action in report["next_actions"])


def test_unknown_watchdog_restart_hook_without_actions_is_not_called_stale() -> None:
    tasks = _healthy_tasks()
    tasks["ETA-Watchdog-Restart"] = {
        "task_name": "ETA-Watchdog-Restart",
        "state": "Unknown",
        "error": "scheduler probe timeout",
    }

    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_blocked_bracket_gate(),
        promotion_audit=_blocked_promotion_gate(),
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=tasks,
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["runtime_ready"] is True
    assert report["runtime"]["tasks"]["stale_supervisor_restart_hooks"] == []


def test_collect_task_status_allows_scheduler_probe_warmup(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_powershell_json(command: str, *, timeout_s: int = 10) -> list[dict[str, object]]:
        captured["command"] = command
        captured["timeout_s"] = timeout_s
        return []

    monkeypatch.setattr(audit, "_run_powershell_json", fake_run_powershell_json)

    audit.collect_task_status()

    assert captured["timeout_s"] >= 30


def test_legacy_8420_listener_is_not_required_for_runtime_ready() -> None:
    ports = _listening_ports()
    ports[8420] = {"port": 8420, "listening": False, "owners": []}

    report = audit.build_report(
        services=_running_services(),
        ports=ports,
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert 8420 not in report["runtime"]["ports"]["required"]
    assert 8420 not in report["runtime"]["ports"]["missing"]
    assert report["summary"]["runtime_ready"] is True


def test_service_config_drift_requires_restart_before_green() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={
            "fm_status_server": {
                "matches_expected": False,
                "expected_executable": r"C:\EvolutionaryTradingAlgo\eta_engine\.venv\Scripts\python.exe",
                "installed_executable": r"C:\OldPython\python.exe",
            }
        },
    )

    assert report["summary"]["status"] == "YELLOW_RESTART_REQUIRED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["promotion_allowed"] is False
    assert any("elevated" in action.lower() for action in report["next_actions"])


def test_stale_dashboard_diagnostics_schema_requires_reload_before_green() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_stale_dashboard_schema_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "YELLOW_RESTART_REQUIRED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["dashboard_schema_current"] is False
    assert report["summary"]["promotion_allowed"] is False
    assert "local_dashboard_api_diagnostics" in report["runtime"]["endpoints"]["schema_drift"]
    assert "local_dashboard_proxy_diagnostics" in report["runtime"]["endpoints"]["schema_drift"]
    assert any("diagnostics schema" in action for action in report["next_actions"])


def test_reads_existing_string_summary_artifact_shapes() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "BLOCKED_UNBRACKETED_EXPOSURE",
            "ready_for_prop_dry_run": False,
            "position_summary": {"missing_bracket_count": 1},
            "unprotected_positions": [{"symbol": "MNQM6"}],
        },
        promotion_audit={
            "summary": "BLOCKED_PAPER_SOAK",
            "ready_for_prop_dry_run_review": False,
        },
        service_config={"fm_status_server": {"matches_expected": True}},
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["safety_gates"]["broker_brackets"]["status"] == "BLOCKED_UNBRACKETED_EXPOSURE"
    assert report["safety_gates"]["broker_brackets"]["missing_bracket_count"] == 1
    assert report["safety_gates"]["promotion"]["status"] == "BLOCKED_PAPER_SOAK"
    assert any("MNQM6" in action for action in report["next_actions"])


def test_ready_no_open_exposure_counts_as_bracket_ready() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["safety_gates"]["broker_brackets"]["ready"] is True
    assert report["summary"]["trading_gate_ready"] is True
    assert report["summary"]["status"] == "GREEN_READY_FOR_SOAK"


def test_ready_open_exposure_bracketed_counts_as_bracket_ready() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_OPEN_EXPOSURE_BRACKETED",
            "ready_for_prop_dry_run": True,
            "position_summary": {
                "broker_bracket_required_position_count": 2,
                "broker_bracket_count": 2,
            },
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["safety_gates"]["broker_brackets"]["ready"] is True
    assert report["summary"]["trading_gate_ready"] is True
    assert report["summary"]["status"] == "GREEN_READY_FOR_SOAK"


def test_supervisor_broker_reconcile_mismatch_blocks_trading_gate() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
        supervisor_reconcile={
            "checked_at": audit.datetime.now(audit.UTC).isoformat(),
            "broker_only": [{"symbol": "MYM", "broker_qty": 1.0}],
            "supervisor_only": [],
            "divergent": [{"symbol": "MNQ", "broker_qty": 3.0, "supervisor_qty": 1.0}],
            "brokers_queried": ["ibkr"],
        },
    )

    reconcile = report["safety_gates"]["supervisor_reconcile"]
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["trading_gate_ready"] is False
    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert reconcile["ready"] is False
    assert reconcile["status"] == "BLOCKED_BROKER_SUPERVISOR_RECONCILE"
    assert reconcile["broker_only_symbols"] == ["MYM"]
    assert reconcile["divergent_symbols"] == ["MNQ"]
    assert any("MYM" in action and "MNQ" in action for action in report["next_actions"])


def test_current_supervisor_reconcile_uses_heartbeat_and_live_broker_state() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
        supervisor_reconcile={
            "checked_at": audit.datetime.now(audit.UTC).isoformat(),
            "broker_only": [{"symbol": "MCL", "broker_qty": 1.0}],
            "supervisor_only": [{"symbol": "MBT", "supervisor_qty": -1.0}],
            "divergent": [],
            "brokers_queried": ["ibkr"],
        },
        supervisor_heartbeat={
            "ts": audit.datetime.now(audit.UTC).isoformat(),
            "bots": [
                {"bot_id": "mnq", "symbol": "MNQ1", "open_position": {"side": "BUY", "qty": 1}},
                {"bot_id": "mbt", "symbol": "MBT1", "open_position": {"side": "BUY", "qty": 1}},
            ],
        },
        live_broker_state={
            "ready": True,
            "source": "cached_live_broker_state_for_diagnostics",
            "ibkr": {
                "ready": True,
                "open_positions": [
                    {"symbol": "MCLM6", "position": 1},
                    {"symbol": "MYMM6", "position": 1},
                    {"symbol": "MNQM6", "position": 3},
                    {"symbol": "MBTK6", "position": 1},
                ],
            },
        },
    )

    reconcile = report["safety_gates"]["supervisor_reconcile"]
    assert reconcile["source"] == "supervisor_heartbeat_and_live_broker_state"
    assert reconcile["broker_only_symbols"] == ["MCL", "MYM"]
    assert reconcile["supervisor_only_symbols"] == []
    assert reconcile["divergent_symbols"] == ["MNQ"]
    assert any("broker-only: MCL, MYM" in action for action in report["next_actions"])


def test_clean_current_reconcile_does_not_clear_prior_startup_latch() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
        supervisor_reconcile={
            "checked_at": audit.datetime.now(audit.UTC).isoformat(),
            "broker_only": [{"symbol": "MCL", "broker_qty": 1.0}],
            "supervisor_only": [],
            "divergent": [],
            "brokers_queried": ["ibkr"],
        },
        supervisor_heartbeat={
            "ts": audit.datetime.now(audit.UTC).isoformat(),
            "bots": [{"bot_id": "mcl", "symbol": "MCL1", "open_position": {"side": "BUY", "qty": 1}}],
        },
        live_broker_state={
            "source": "cached_live_broker_state_for_diagnostics",
            "ibkr": {"ready": True, "open_positions": [{"symbol": "MCLM6", "position": 1}]},
        },
    )

    reconcile = report["safety_gates"]["supervisor_reconcile"]
    assert reconcile["source"] == "reconcile_artifact"
    assert reconcile["broker_only_symbols"] == ["MCL"]
    assert report["summary"]["trading_gate_ready"] is False


def test_missing_live_broker_detail_does_not_create_false_supervisor_only() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
        supervisor_reconcile={
            "checked_at": audit.datetime.now(audit.UTC).isoformat(),
            "broker_only": [{"symbol": "MCL", "broker_qty": 1.0}],
            "supervisor_only": [],
            "divergent": [],
            "brokers_queried": ["ibkr"],
        },
        supervisor_heartbeat={
            "ts": audit.datetime.now(audit.UTC).isoformat(),
            "bots": [
                {"bot_id": "mnq", "symbol": "MNQ1", "open_position": {"side": "BUY", "qty": 1}},
                {"bot_id": "mbt", "symbol": "MBT1", "open_position": {"side": "BUY", "qty": 1}},
            ],
        },
        live_broker_state={"ready": False, "source": "probe_failed"},
    )

    reconcile = report["safety_gates"]["supervisor_reconcile"]
    assert reconcile["source"] == "reconcile_artifact"
    assert reconcile["broker_only_symbols"] == ["MCL"]
    assert reconcile["supervisor_only_symbols"] == []


def test_missing_supervisor_code_revision_blocks_trading_gate() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
        supervisor_heartbeat={
            "ts": audit.datetime.now(audit.UTC).isoformat(),
            "bots": [],
        },
        repo_revision={"head": "abc1234", "head_short": "abc1234"},
    )

    supervisor_code = report["safety_gates"]["supervisor_code"]
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["trading_gate_ready"] is False
    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert supervisor_code["status"] == "MISSING_SUPERVISOR_CODE_REVISION"
    assert supervisor_code["ready"] is False
    assert any("Restart ETA-Jarvis-Strategy-Supervisor" in action for action in report["next_actions"])


def test_stale_supervisor_code_revision_blocks_trading_gate() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
        supervisor_heartbeat={
            "ts": audit.datetime.now(audit.UTC).isoformat(),
            "code_revision": {"head": "oldrev", "head_short": "oldrev"},
            "bots": [],
        },
        repo_revision={"head": "newrev", "head_short": "newrev"},
    )

    supervisor_code = report["safety_gates"]["supervisor_code"]
    assert report["summary"]["trading_gate_ready"] is False
    assert supervisor_code["status"] == "STALE_SUPERVISOR_CODE"
    assert supervisor_code["ready"] is False
    assert supervisor_code["heartbeat_head"] == "oldrev"
    assert supervisor_code["repo_head"] == "newrev"
    assert any("oldrev" in action and "newrev" in action for action in report["next_actions"])


def test_dashboard_ports_live_but_durable_tasks_missing_is_yellow_gap() -> None:
    tasks = _healthy_tasks()
    for name in audit.DASHBOARD_DURABLE_TASKS:
        tasks[name] = {"task_name": name, "state": "Missing"}
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=tasks,
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "YELLOW_DURABILITY_GAP"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["dashboard_durable"] is False
    assert report["summary"]["promotion_allowed"] is False
    assert "ETA-Dashboard-API" in report["runtime"]["tasks"]["missing_dashboard_durable"]
    assert "ETA-OperatorQueueHeartbeat" in report["runtime"]["tasks"]["missing_dashboard_durable"]
    assert "ETA-BrokerStateRefreshHeartbeat" in report["runtime"]["tasks"]["missing_dashboard_durable"]
    assert "ETA-PaperLiveTransitionCheck" in report["runtime"]["tasks"]["missing_dashboard_durable"]
    assert any("repair_dashboard_durability_admin.cmd" in action for action in report["next_actions"])


def test_broker_state_refresh_heartbeat_is_dashboard_durability_task() -> None:
    assert "ETA-BrokerStateRefreshHeartbeat" in audit.DASHBOARD_DURABLE_TASKS


def test_missing_symbol_intelligence_collector_degrades_runtime() -> None:
    tasks = _healthy_tasks()
    tasks["ETA-SymbolIntelCollector"] = {
        "task_name": "ETA-SymbolIntelCollector",
        "state": "Missing",
        "last_task_result": None,
    }

    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=tasks,
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "RED_RUNTIME_DEGRADED"
    assert report["summary"]["runtime_ready"] is False
    assert report["runtime"]["tasks"]["data_pipeline"] == ["ETA-SymbolIntelCollector"]
    assert report["runtime"]["tasks"]["missing_data_pipeline"] == ["ETA-SymbolIntelCollector"]
    assert any("data-pipeline" in action for action in report["next_actions"])


def test_missing_ibc_credentials_blocks_trading_gate_without_red_runtime() -> None:
    ports = _listening_ports()
    ports[4002] = {"port": 4002, "listening": False, "owners": []}

    report = audit.build_report(
        services=_running_services(),
        ports=ports,
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={
            "status": "missing_ibc_credentials",
            "reason": "IBC recovery task is configured, but usable credentials are missing.",
        },
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["trading_gate_ready"] is False
    assert report["broker_runtime"]["ibgateway"]["status"] == "missing_ibc_credentials"
    assert report["broker_runtime"]["ibgateway"]["port_listening"] is False
    assert any("set_ibc_credentials.ps1" in action for action in report["next_actions"])


def test_non_authoritative_gateway_host_points_operator_to_vps_not_local_repair() -> None:
    ports = _listening_ports()
    ports[4002] = {"port": 4002, "listening": False, "owners": []}

    report = audit.build_report(
        services=_running_services(),
        ports=ports,
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={
            "status": "non_authoritative_gateway_host",
            "reason": "Refusing IBKR Gateway recovery on this host because the VPS is the 24/7 Gateway authority.",
            "gateway_authority": {
                "allowed": False,
                "source": "missing_marker",
                "computer_name": "ETA",
            },
        },
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["trading_gate_ready"] is False
    assert report["broker_runtime"]["ibgateway"]["non_authoritative_host"] is True
    assert report["broker_runtime"]["ibgateway"]["gateway_authority"]["source"] == "missing_marker"
    assert any("Do not enable local desktop Gateway tasks" in action for action in report["next_actions"])
    assert not any("Gateway API port 4002 is listening" in action for action in report["next_actions"])


def test_admin_ai_warn_blocks_green_without_marking_runtime_red() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
        jarvis_hermes_admin={
            "status": "WARN",
            "summary": {
                "admin_ai_ready": False,
                "checks": 8,
                "pass": 7,
                "warnings": 1,
                "blocked": 0,
            },
            "next_actions": ["Review bridge_plan_tasks: T17 wave is not fully represented yet"],
        },
    )

    assert report["summary"]["status"] == "YELLOW_ADMIN_AI_PENDING"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["trading_gate_ready"] is True
    assert report["summary"]["admin_ai_ready"] is False
    assert report["summary"]["promotion_allowed"] is False
    assert report["safety_gates"]["jarvis_hermes_admin_ai"]["status"] == "WARN"
    assert any("T17 wave" in action for action in report["next_actions"])


def test_admin_ai_blocked_surfaces_safety_gate_without_allowing_orders() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
        jarvis_hermes_admin={
            "status": "BLOCKED",
            "summary": {
                "admin_ai_ready": False,
                "checks": 8,
                "pass": 6,
                "warnings": 0,
                "blocked": 2,
            },
            "next_actions": ["Fix mcp_destructive_safety: missing confirm marker"],
        },
    )

    assert report["summary"]["status"] == "YELLOW_ADMIN_AI_BLOCKED"
    assert report["summary"]["admin_ai_status"] == "BLOCKED"
    assert report["summary"]["promotion_allowed"] is False
    assert report["summary"]["order_action_allowed"] is False
    assert report["safety_gates"]["jarvis_hermes_admin_ai"]["blocked"] == 2
    assert any("missing confirm marker" in action for action in report["next_actions"])


def test_collect_admin_ai_uses_current_bridge_task_set(monkeypatch) -> None:
    """The live hardening audit should track the current bridge plan, not stale T17 wording."""
    captured: dict[str, object] = {}

    def fake_run_audit(workspace_root, *, expected_task_count: int, probe_port: bool) -> dict:
        captured["workspace_root"] = workspace_root
        captured["expected_task_count"] = expected_task_count
        captured["probe_port"] = probe_port
        return {
            "status": "PASS",
            "summary": {
                "admin_ai_ready": True,
                "checks": 8,
                "pass": 8,
                "warnings": 0,
                "blocked": 0,
            },
            "next_actions": [],
        }

    monkeypatch.setattr(audit.jarvis_hermes_admin_audit, "run_audit", fake_run_audit)

    report = audit.collect_jarvis_hermes_admin_status()

    assert report["status"] == "PASS"
    assert captured["expected_task_count"] == 8
    assert captured["probe_port"] is True
