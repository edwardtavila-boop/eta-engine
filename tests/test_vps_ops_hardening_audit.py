from __future__ import annotations

import json

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


def _modern_dashboard_schema_endpoints() -> dict[str, dict[str, object]]:
    endpoints = _healthy_endpoints()
    modern_payload = {
        "command_center_watchdog": {"status": "healthy"},
        "surface_watch": {"status": "healthy"},
        "eta_readiness_snapshot": {"status": "blocked"},
        "daily_stop_reset_audit": {"status": "stale_receipt"},
        "vps_ops_hardening": {
            "status": "YELLOW_SAFETY_BLOCKED",
            "summary": {"runtime_ready": True},
        },
        "hardening": {
            "status": "YELLOW_SAFETY_BLOCKED",
            "summary": {"runtime_ready": True},
        },
        "checks": {
            "command_center_watchdog_contract": True,
            "eta_readiness_snapshot_contract": True,
            "daily_stop_reset_audit_contract": True,
            "hardening_contract": True,
            "vps_ops_hardening_contract": True,
        },
    }
    modern_payload["surface_watch"] = modern_payload["command_center_watchdog"]
    modern_payload["hardening"] = modern_payload["vps_ops_hardening"]
    endpoints["local_dashboard_api_diagnostics"] = {
        "ok": True,
        "status_code": 200,
        "payload": modern_payload,
    }
    endpoints["local_dashboard_proxy_diagnostics"] = {
        "ok": True,
        "status_code": 200,
        "payload": modern_payload,
    }
    return endpoints


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
        + audit.FORCE_MULTIPLIER_DURABLE_TASKS
        + audit.IBGATEWAY_TASKS
    }


def _dashboard_tasks_access_denied(tasks: dict[str, dict[str, object]] | None = None) -> dict[str, dict[str, object]]:
    observed = dict(tasks or _healthy_tasks())
    for name in audit.DASHBOARD_DURABLE_TASKS:
        observed[name] = {
            "task_name": name,
            "state": "AccessDenied",
            "last_task_result": None,
            "error": "Access is denied.",
            "query_source": "schtasks",
        }
    return observed


def _non_authoritative_task_artifacts(
    *,
    broker_state_refresh_status: str | None = None,
    supervisor_reconcile_status: str = "fresh",
    operator_queue_status: str = "fresh",
    paper_live_transition_status: str = "fresh",
    force_multiplier_status: str = "fresh",
    paper_live_status: str = "fresh",
    data_pipeline_status: str = "fresh",
) -> dict[str, dict[str, object]]:
    coverage = {
        "ETA-SupervisorBrokerReconcile": {
            "task_name": "ETA-SupervisorBrokerReconcile",
            "covered": supervisor_reconcile_status in {"fresh", "stale"},
            "stale": supervisor_reconcile_status == "stale",
            "status": supervisor_reconcile_status,
            "path": "C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_intel/supervisor/reconcile_last.json",
            "source": "supervisor_reconcile",
            "age_s": 1800.0 if supervisor_reconcile_status == "stale" else 60.0,
            "max_age_s": 900,
        },
        "ETA-OperatorQueueHeartbeat": {
            "task_name": "ETA-OperatorQueueHeartbeat",
            "covered": operator_queue_status in {"fresh", "stale"},
            "stale": operator_queue_status == "stale",
            "status": operator_queue_status,
            "path": "C:/EvolutionaryTradingAlgo/var/eta_engine/state/operator_queue_snapshot.json",
            "source": "operator_queue_snapshot",
            "age_s": 25200.0 if operator_queue_status == "stale" else 60.0,
            "max_age_s": 21600,
        },
        "ETA-PaperLiveTransitionCheck": {
            "task_name": "ETA-PaperLiveTransitionCheck",
            "covered": paper_live_transition_status in {"fresh", "stale"},
            "stale": paper_live_transition_status == "stale",
            "status": paper_live_transition_status,
            "path": "C:/EvolutionaryTradingAlgo/var/eta_engine/state/paper_live_transition_check.json",
            "source": "paper_live_transition_check",
            "age_s": 64800.0 if paper_live_transition_status == "stale" else 300.0,
            "max_age_s": 43200,
        },
        "ETA-ThreeAI-Sync": {
            "task_name": "ETA-ThreeAI-Sync",
            "covered": force_multiplier_status in {"fresh", "stale"},
            "stale": force_multiplier_status == "stale",
            "status": force_multiplier_status,
            "path": "C:/EvolutionaryTradingAlgo/var/eta_engine/state/fm_health.json",
            "source": "fm_health_snapshot",
            "age_s": 64800.0 if force_multiplier_status == "stale" else 300.0,
            "max_age_s": 43200,
        },
        "ETA-PaperLive-Supervisor": {
            "task_name": "ETA-PaperLive-Supervisor",
            "covered": paper_live_status in {"fresh", "stale"},
            "stale": paper_live_status == "stale",
            "status": paper_live_status,
            "path": "C:/EvolutionaryTradingAlgo/var/eta_engine/state/paper_live_transition_check.json",
            "source": "paper_live_transition_check",
            "age_s": 64800.0 if paper_live_status == "stale" else 300.0,
            "max_age_s": 43200,
        },
        "ETA-IndexFutures-Bar-Refresh": {
            "task_name": "ETA-IndexFutures-Bar-Refresh",
            "covered": data_pipeline_status in {"fresh", "stale"},
            "stale": data_pipeline_status == "stale",
            "status": data_pipeline_status,
            "path": "C:/EvolutionaryTradingAlgo/var/eta_engine/state/symbol_intelligence_latest.json",
            "source": "symbol_intelligence_snapshot",
            "age_s": 25200.0 if data_pipeline_status == "stale" else 300.0,
            "max_age_s": 21600,
        },
    }
    if broker_state_refresh_status in {"fresh", "stale"}:
        coverage["ETA-BrokerStateRefreshHeartbeat"] = {
            "task_name": "ETA-BrokerStateRefreshHeartbeat",
            "covered": True,
            "stale": broker_state_refresh_status == "stale",
            "status": broker_state_refresh_status,
            "path": "http://127.0.0.1:8421/api/live/broker_state?refresh=1",
            "source": "live_broker_rest",
            "age_s": 1800.0 if broker_state_refresh_status == "stale" else 15.0,
            "max_age_s": 900,
            "broker_snapshot_state": broker_state_refresh_status,
            "broker_ready": False,
        }
    return coverage


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


def test_promotion_blocker_points_to_broker_paper_capture_after_positive_shadow_outcomes() -> None:
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
                "shadow_signal_evidence": {"signal_count": 40},
                "shadow_outcome_evidence": {
                    "evaluated_count": 33,
                    "verdict": "POSITIVE_COUNTERFACTUAL_EDGE",
                    "broker_backed": False,
                    "promotion_proof": False,
                },
            },
        },
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert any("broker-paper close capture" in action for action in report["next_actions"])
    assert not any("shadow signals into paper-close outcomes" in action for action in report["next_actions"])


def test_promotion_blocker_includes_retune_command_after_weak_shadow_outcomes() -> None:
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
                "shadow_signal_evidence": {"signal_count": 88},
                "shadow_outcome_evidence": {
                    "shadow_signal_count": 88,
                    "evaluated_count": 88,
                    "verdict": "WEAK_OR_NEGATIVE_COUNTERFACTUAL",
                    "broker_backed": False,
                    "promotion_proof": False,
                },
                "retune_plan": {
                    "status": "PAPER_ONLY_RETUNE_REQUIRED",
                    "retune_command": (
                        "python -m eta_engine.scripts.run_research_grid "
                        "--source registry --bots volume_profile_nq --report-policy runtime"
                    ),
                    "safe_to_mutate_live": False,
                },
            },
        },
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert any("Retune runner-up volume_profile_nq" in action for action in report["next_actions"])
    assert any(
        "run_research_grid --source registry --bots volume_profile_nq" in action
        for action in report["next_actions"]
    )
    assert not any("broker-paper close capture" in action for action in report["next_actions"])


def test_promotion_blocker_points_to_fresh_shadow_context_when_replay_lacks_context() -> None:
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
                "shadow_signal_evidence": {"signal_count": 40},
                "shadow_outcome_evidence": {
                    "shadow_signal_count": 40,
                    "evaluated_count": 0,
                    "missing_context": 40,
                    "verdict": "NO_EVALUATED_SIGNALS",
                    "broker_backed": False,
                    "promotion_proof": False,
                },
            },
        },
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert any("fresh bracket-context shadow signals" in action for action in report["next_actions"])
    assert not any("Repair bar freshness" in action for action in report["next_actions"])


def test_promotion_blocker_points_to_stale_replay_coverage_after_failed_shadow_replay() -> None:
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
                "shadow_signal_evidence": {"signal_count": 40},
                "shadow_outcome_evidence": {
                    "shadow_signal_count": 40,
                    "evaluated_count": 0,
                    "missing_bars": 40,
                    "no_bar_after_signal": 40,
                    "latest_bar_coverage_end_ts": "2026-05-08T10:50:00+00:00",
                    "verdict": "NO_EVALUATED_SIGNALS",
                    "broker_backed": False,
                    "promotion_proof": False,
                },
                "next_action": (
                    "Refresh NQ1 5-minute replay bars for volume_profile_nq; latest available replay "
                    "bar is 2026-05-08T10:50:00+00:00 and 40 shadow signals arrived after coverage ended"
                ),
            },
        },
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert any("Refresh NQ1 5-minute replay bars" in action for action in report["next_actions"])
    assert not any("shadow signals into paper-close outcomes" in action for action in report["next_actions"])


def test_promotion_blocker_prioritizes_no_reviewable_runner_over_primary_retirement() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit=_ready_bracket_gate(),
        promotion_audit={
            "summary": "BLOCKED_KAIZEN_RETIRED",
            "ready_for_prop_dry_run_review": False,
            "next_runner_candidate": {},
            "required_evidence": [
                "review Kaizen retirement evidence for volume_profile_mnq",
                "document deactivation reason: tier=DECAY",
                "no runner-up candidate is promotion-reviewable; keep all candidates paper/research-only",
            ],
        },
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert any("no runner-up candidate is promotion-reviewable" in action for action in report["next_actions"])
    assert not any(
        action.endswith("review Kaizen retirement evidence for volume_profile_mnq")
        for action in report["next_actions"]
    )


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


def test_stale_flat_open_orders_block_paper_live_gate_with_symbol_action() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={
            "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
            "ready_for_prop_dry_run": False,
            "position_summary": {
                "missing_bracket_count": 0,
                "stale_flat_open_order_count": 2,
                "stale_flat_open_order_symbols": ["MCLM6", "MYMM6"],
            },
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["paper_live_gate_ready"] is False
    assert report["summary"]["trading_gate_ready"] is False
    assert report["safety_gates"]["broker_brackets"]["ready"] is False
    assert report["safety_gates"]["broker_brackets"]["stale_flat_open_order_count"] == 2
    assert report["safety_gates"]["broker_brackets"]["stale_flat_open_order_symbols"] == ["MCLM6", "MYMM6"]
    assert any("Cancel stale broker open orders for MCLM6, MYMM6" in action for action in report["next_actions"])


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


def test_live_fm_endpoint_with_non_running_service_is_restart_required_not_red() -> None:
    services = _running_services()
    services["FmStatusServer"] = {
        "name": "FmStatusServer",
        "status": "StartPending",
        "start_type": "Automatic",
    }
    ports = _listening_ports()
    ports[8422] = {
        "port": 8422,
        "listening": True,
        "owners": [30980],
        "owner_details": [
            {
                "Pid": 30980,
                "Name": "python",
                "Path": "C:/Python314/python.exe",
                "CommandLine": '"C:/Python314/python.exe" -m eta_engine.deploy.fm_status_server',
            }
        ],
    }

    report = audit.build_report(
        services=services,
        ports=ports,
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "YELLOW_RESTART_REQUIRED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["service_down"] == []
    assert report["summary"]["service_runtime_drift"] == ["FmStatusServer"]
    assert report["summary"]["service_config_drift"] == []
    assert report["summary"]["missing_ports"] == []
    assert report["summary"]["critical_endpoint_failures"] == []
    assert report["runtime"]["services"]["down"] == []
    assert report["runtime"]["services"]["runtime_drift"] == ["FmStatusServer"]
    assert report["runtime"]["services"]["runtime_drift_detail"]["FmStatusServer"]["port_owner_details"] == [
        {
            "Pid": 30980,
            "Name": "python",
            "Path": "C:/Python314/python.exe",
            "CommandLine": '"C:/Python314/python.exe" -m eta_engine.deploy.fm_status_server',
        }
    ]
    assert (
        report["runtime"]["services"]["runtime_drift_detail"]["FmStatusServer"]["port_owner_runner"]
        == "manual_module_runner"
    )
    assert (
        report["runtime"]["services"]["runtime_drift_detail"]["FmStatusServer"]["port_owner_runner_label"]
        == "manual module runner"
    )
    assert any(
        "live endpoint: FmStatusServer (port 8422 owner=python, manual module runner)" in action
        for action in report["next_actions"]
    )
    assert any("repair_force_multiplier_control_plane_admin.cmd /RestartService" in action for action in report["next_actions"])


def test_missing_force_multiplier_sync_task_surfaces_control_plane_durability_gap() -> None:
    tasks = _healthy_tasks()
    tasks["ETA-ThreeAI-Sync"] = {"task_name": "ETA-ThreeAI-Sync", "state": "Missing", "last_task_result": None}

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
    assert report["summary"]["force_multiplier_durable"] is False
    assert report["runtime"]["tasks"]["force_multiplier_durable"] == ["ETA-ThreeAI-Sync"]
    assert report["runtime"]["tasks"]["missing_force_multiplier_durable"] == ["ETA-ThreeAI-Sync"]
    assert any("Force Multiplier scheduled task lane" in action for action in report["next_actions"])
    assert any("repair_force_multiplier_control_plane_admin.cmd /RestartService" in action for action in report["next_actions"])


def test_non_authoritative_cached_force_multiplier_artifact_downgrades_local_missing_task() -> None:
    tasks = _healthy_tasks()
    tasks["ETA-ThreeAI-Sync"] = {"task_name": "ETA-ThreeAI-Sync", "state": "Missing", "last_task_result": None}

    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=tasks,
        ibgateway_reauth={
            "status": "non_authoritative_gateway_host",
            "reason": "Refusing IBKR Gateway recovery on this host because the VPS is the 24/7 Gateway authority.",
            "gateway_authority": {
                "allowed": False,
                "source": "missing_marker",
                "computer_name": "ETA",
            },
        },
        non_authoritative_task_artifacts=_non_authoritative_task_artifacts(force_multiplier_status="stale"),
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["force_multiplier_durable"] is True
    assert report["runtime"]["tasks"]["observed_missing_force_multiplier_durable"] == ["ETA-ThreeAI-Sync"]
    assert report["runtime"]["tasks"]["missing_force_multiplier_durable"] == []
    assert report["runtime"]["tasks"]["artifact_backed_missing_force_multiplier_durable"] == ["ETA-ThreeAI-Sync"]
    assert report["runtime"]["tasks"]["stale_artifact_backed_force_multiplier_durable"] == ["ETA-ThreeAI-Sync"]
    assert any("non-authoritative Force Multiplier watch artifacts" in action for action in report["next_actions"])
    assert not any("repair_force_multiplier_control_plane_admin.cmd" in action for action in report["next_actions"])


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


def test_non_authoritative_cached_paper_live_artifact_downgrades_local_missing_task() -> None:
    tasks = _healthy_tasks()
    tasks["ETA-PaperLive-Supervisor"] = {
        "task_name": "ETA-PaperLive-Supervisor",
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
        ibgateway_reauth={
            "status": "non_authoritative_gateway_host",
            "reason": "Refusing IBKR Gateway recovery on this host because the VPS is the 24/7 Gateway authority.",
            "gateway_authority": {
                "allowed": False,
                "source": "missing_marker",
                "computer_name": "ETA",
            },
        },
        non_authoritative_task_artifacts=_non_authoritative_task_artifacts(paper_live_status="stale"),
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["runtime_ready"] is True
    assert report["runtime"]["tasks"]["observed_missing_paper_live_durable"] == ["ETA-PaperLive-Supervisor"]
    assert report["runtime"]["tasks"]["missing_paper_live_durable"] == []
    assert report["runtime"]["tasks"]["artifact_backed_missing_paper_live_durable"] == ["ETA-PaperLive-Supervisor"]
    assert report["runtime"]["tasks"]["stale_artifact_backed_paper_live_durable"] == ["ETA-PaperLive-Supervisor"]
    assert any("non-authoritative paper-live watch artifacts" in action for action in report["next_actions"])
    assert not any("Repair paper-live scheduled task lane" in action for action in report["next_actions"])


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
    assert "schtasks.exe /query /tn $name /fo LIST /v" in str(captured["command"])


def test_collect_task_status_preserves_access_denied_rows(monkeypatch) -> None:
    def fake_run_powershell_json(command: str, *, timeout_s: int = 10) -> list[dict[str, object]]:
        return [
            {
                "TaskName": "ETA-Dashboard-API",
                "State": "AccessDenied",
                "LastTaskResult": None,
                "LastRunTime": None,
                "NextRunTime": None,
                "Actions": None,
                "Error": "Access is denied.",
                "QuerySource": "schtasks",
            },
            {
                "TaskName": "ETA-PaperLive-Supervisor",
                "State": "Missing",
                "LastTaskResult": None,
                "LastRunTime": None,
                "NextRunTime": None,
                "Actions": None,
                "Error": "The system cannot find the file specified.",
                "QuerySource": "schtasks",
            },
        ]

    monkeypatch.setattr(audit, "_run_powershell_json", fake_run_powershell_json)

    observed = audit.collect_task_status()

    assert observed["ETA-Dashboard-API"]["state"] == "AccessDenied"
    assert observed["ETA-Dashboard-API"]["error"] == "Access is denied."
    assert observed["ETA-Dashboard-API"]["query_source"] == "schtasks"
    assert observed["ETA-PaperLive-Supervisor"]["state"] == "Missing"
    assert observed["ETA-PaperLive-Supervisor"]["error"] == "The system cannot find the file specified."


def test_collect_endpoint_status_honors_per_endpoint_timeouts(monkeypatch) -> None:
    captured: list[tuple[str, float]] = []

    def fake_probe(
        url: str,
        *,
        timeout_s: float = 8.0,
        max_bytes: int = audit.DEFAULT_ENDPOINT_READ_MAX_BYTES,
    ) -> dict[str, object]:
        captured.append((url, timeout_s))
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(audit, "_probe_endpoint", fake_probe)
    monkeypatch.setattr(
        audit,
        "ENDPOINTS",
        (
            {
                "name": "slow_diagnostics",
                "url": "http://127.0.0.1:8000/api/dashboard/diagnostics",
                "critical": True,
                "timeout_s": 15.0,
            },
            {
                "name": "default_probe",
                "url": "http://127.0.0.1:8422/api/fm/status",
                "critical": True,
            },
        ),
    )

    observed = audit.collect_endpoint_status()

    assert observed["slow_diagnostics"]["critical"] is True
    assert observed["default_probe"]["critical"] is True
    assert captured == [
        ("http://127.0.0.1:8000/api/dashboard/diagnostics", 15.0),
        ("http://127.0.0.1:8422/api/fm/status", 8.0),
    ]


def test_collect_endpoint_status_retries_configured_transient_failure(monkeypatch) -> None:
    captured: list[tuple[str, float]] = []
    attempts_by_url: dict[str, int] = {}

    def fake_probe(
        url: str,
        *,
        timeout_s: float = 8.0,
        max_bytes: int = audit.DEFAULT_ENDPOINT_READ_MAX_BYTES,
    ) -> dict[str, object]:
        captured.append((url, timeout_s))
        attempts_by_url[url] = attempts_by_url.get(url, 0) + 1
        if url.endswith("/api/dashboard/diagnostics") and attempts_by_url[url] == 1:
            return {"ok": False, "error": "timed out"}
        return {"ok": True, "status_code": 200}

    monkeypatch.setattr(audit, "_probe_endpoint", fake_probe)
    monkeypatch.setattr(
        audit,
        "ENDPOINTS",
        (
            {
                "name": "retrying_diagnostics",
                "url": "http://127.0.0.1:8000/api/dashboard/diagnostics",
                "critical": True,
                "timeout_s": 15.0,
                "retries": 1,
            },
            {
                "name": "single_probe",
                "url": "http://127.0.0.1:8422/api/fm/status",
                "critical": True,
            },
        ),
    )

    observed = audit.collect_endpoint_status()

    assert observed["retrying_diagnostics"]["ok"] is True
    assert observed["retrying_diagnostics"]["status_code"] == 200
    assert observed["single_probe"]["ok"] is True
    assert captured == [
        ("http://127.0.0.1:8000/api/dashboard/diagnostics", 15.0),
        ("http://127.0.0.1:8000/api/dashboard/diagnostics", 15.0),
        ("http://127.0.0.1:8422/api/fm/status", 8.0),
    ]


def test_collect_live_report_reads_large_broker_state_payload(monkeypatch) -> None:
    captured: dict[str, object] = {"probes": []}
    base_payload = {
        "ready": True,
        "source": "cached_live_broker_state_for_diagnostics",
        "broker_snapshot_state": "missing",
        "server_ts": audit.datetime.now(audit.UTC).timestamp(),
        "ibkr": {"ready": True, "open_positions": []},
    }
    refresh_payload = {
        "ready": True,
        "source": "live_broker_rest",
        "broker_snapshot_state": "fresh",
        "server_ts": audit.datetime.now(audit.UTC).timestamp(),
        "ibkr": {"ready": True, "open_positions": []},
    }

    def fake_probe(
        url: str,
        *,
        timeout_s: float = 8.0,
        max_bytes: int = audit.DEFAULT_ENDPOINT_READ_MAX_BYTES,
    ) -> dict[str, object]:
        captured["probes"].append(
            {
                "url": url,
                "timeout_s": timeout_s,
                "max_bytes": max_bytes,
            }
        )
        payload = refresh_payload if url == audit.BROKER_STATE_REFRESH_URL else base_payload
        return {"ok": True, "status_code": 200, "payload": payload}

    def fake_collect_non_authoritative_task_artifacts(*, now=None, live_broker_state=None):
        captured["artifact_live_broker_state"] = live_broker_state
        return {"ETA-BrokerStateRefreshHeartbeat": {"covered": True, "stale": False}}

    def fake_build_report(**kwargs):
        captured["build_live_broker_state"] = kwargs.get("live_broker_state")
        captured["build_non_authoritative_task_artifacts"] = kwargs.get("non_authoritative_task_artifacts")
        return {"summary": {"status": "GREEN_READY_FOR_SOAK"}, "next_actions": []}

    monkeypatch.setattr(audit, "_probe_endpoint", fake_probe)
    monkeypatch.setattr(audit, "collect_service_status", lambda: {})
    monkeypatch.setattr(audit, "collect_port_status", lambda: {})
    monkeypatch.setattr(audit, "collect_endpoint_status", lambda: {})
    monkeypatch.setattr(audit, "collect_service_config_status", lambda: {})
    monkeypatch.setattr(audit, "collect_task_status", lambda: {})
    monkeypatch.setattr(audit, "collect_jarvis_hermes_admin_status", lambda: {})
    monkeypatch.setattr(audit, "collect_repo_revision", lambda: {})
    monkeypatch.setattr(audit, "_read_json", lambda path: {})
    monkeypatch.setattr(
        audit,
        "collect_non_authoritative_task_artifacts",
        fake_collect_non_authoritative_task_artifacts,
    )
    monkeypatch.setattr(audit, "build_report", fake_build_report)

    report = audit.collect_live_report()

    assert report["summary"]["status"] == "GREEN_READY_FOR_SOAK"
    assert captured["probes"] == [
        {
            "url": audit.BROKER_STATE_URL,
            "timeout_s": 8.0,
            "max_bytes": audit.BROKER_STATE_READ_MAX_BYTES,
        },
        {
            "url": audit.BROKER_STATE_REFRESH_URL,
            "timeout_s": 8.0,
            "max_bytes": audit.BROKER_STATE_READ_MAX_BYTES,
        },
    ]
    assert captured["artifact_live_broker_state"] == refresh_payload
    assert captured["build_live_broker_state"] == refresh_payload
    assert captured["build_non_authoritative_task_artifacts"] == {
        "ETA-BrokerStateRefreshHeartbeat": {"covered": True, "stale": False}
    }


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
    assert report["summary"]["service_runtime_drift"] == []
    assert report["summary"]["service_config_drift"] == ["fm_status_server"]
    assert any("elevated" in action.lower() for action in report["next_actions"])


def test_collect_service_config_status_uses_resolved_python_for_fm_status_server(tmp_path, monkeypatch) -> None:
    eta_engine_root = tmp_path / "eta_engine"
    install_root = tmp_path / "firm_command_center" / "services"
    template_xml = eta_engine_root / "deploy" / "FmStatusServer.xml"
    installed_xml = install_root / "FmStatusServer" / "FmStatusServer.xml"
    legacy_installed_xml = install_root / "FmStatusServer.xml"
    template_xml.parent.mkdir(parents=True)
    installed_xml.parent.mkdir(parents=True)
    template_xml.write_text(
        "<service><executable>C:\\EvolutionaryTradingAlgo\\eta_engine\\.venv\\Scripts\\python.exe</executable>"
        "<arguments>-m eta_engine.deploy.fm_status_http_server --host 127.0.0.1 --port 8422</arguments></service>",
        encoding="utf-8",
    )
    installed_xml.write_text(
        "<service><executable>C:\\Python314\\python.exe</executable>"
        "<arguments>-m eta_engine.deploy.fm_status_http_server --host 127.0.0.1 --port 8422</arguments></service>",
        encoding="utf-8",
    )
    legacy_installed_xml.write_text(
        "<service><executable>C:\\OldPython\\python.exe</executable>"
        "<arguments>-m eta_engine.deploy.fm_status_http_server --host 127.0.0.1 --port 8422</arguments></service>",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit.workspace_roots, "ETA_ENGINE_ROOT", eta_engine_root)
    monkeypatch.setattr(audit.workspace_roots, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(audit, "FM_STATUS_TEMPLATE_XML", template_xml)
    monkeypatch.setattr(audit, "FM_STATUS_INSTALLED_XML", installed_xml)
    monkeypatch.setattr(audit, "FM_STATUS_INSTALLED_XML_LEGACY", legacy_installed_xml)
    monkeypatch.setattr(audit, "_resolve_eta_python", lambda: audit.DEFAULT_MACHINE_PYTHON)

    observed = audit.collect_service_config_status()

    assert observed["fm_status_server"]["matches_expected"] is True
    assert observed["fm_status_server"]["template_executable"] == r"C:\EvolutionaryTradingAlgo\eta_engine\.venv\Scripts\python.exe"
    assert observed["fm_status_server"]["expected_executable"] == r"C:\Python314\python.exe"
    assert observed["fm_status_server"]["installed_executable"] == r"C:\Python314\python.exe"
    assert observed["fm_status_server"]["expected_executable_source"] == "resolved_python"
    assert observed["fm_status_server"]["installed_xml_source"] == "service_sidecar"


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
    assert any(
        "reload-command-center-admin.cmd -SkipPublicCheck -SkipWatchdogRegistration" in action
        for action in report["next_actions"]
    )


def test_dashboard_diagnostics_without_fm_control_plane_is_still_current() -> None:
    endpoints = _modern_dashboard_schema_endpoints()
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=endpoints,
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["dashboard_schema_current"] is True
    assert report["runtime"]["endpoints"]["schema_drift"] == []
    assert not any("diagnostics schema" in action for action in report["next_actions"])


def test_current_dashboard_diagnostics_schema_is_not_flagged_as_drift() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_modern_dashboard_schema_endpoints(),
        broker_bracket_audit={
            "summary": "READY_NO_OPEN_EXPOSURE",
            "ready_for_prop_dry_run": True,
        },
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_healthy_tasks(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["dashboard_schema_current"] is True
    assert report["runtime"]["endpoints"]["schema_drift"] == []
    assert not any("compatibility aliases and audit contracts" in action for action in report["next_actions"])


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


def test_supervisor_only_local_paper_reconcile_does_not_block_trading_gate() -> None:
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
            "broker_only": [],
            "supervisor_only": [{"symbol": "MBT", "supervisor_qty": 1.0}],
            "divergent": [],
            "mismatch_count": 1,
            "blocking_mismatch_count": 0,
            "ready": True,
            "brokers_queried": ["ibkr"],
        },
    )

    reconcile = report["safety_gates"]["supervisor_reconcile"]
    assert reconcile["ready"] is True
    assert reconcile["status"] == "PASS_SUPERVISOR_ONLY_LOCAL_PAPER"
    assert reconcile["supervisor_only_symbols"] == ["MBT"]
    assert reconcile["mismatch_count"] == 1
    assert reconcile["blocking_mismatch_count"] == 0
    assert report["summary"]["paper_live_gate_ready"] is True
    assert report["summary"]["trading_gate_ready"] is True
    assert not any("reconcile broker/supervisor" in action for action in report["next_actions"])


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


def test_fresh_non_authoritative_zero_position_broker_state_clears_stale_supervisor_reconcile() -> None:
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
        ibgateway_reauth={
            "status": "non_authoritative_gateway_host",
            "reason": "Refusing IBKR Gateway recovery on this host because the VPS is the 24/7 Gateway authority.",
            "gateway_authority": {
                "allowed": False,
                "source": "missing_marker",
                "computer_name": "ETA",
            },
        },
        supervisor_reconcile={
            "checked_at": "2026-05-15T18:21:57.226967+00:00",
            "broker_only": [],
            "supervisor_only": [],
            "divergent": [],
            "brokers_queried": ["ibkr"],
        },
        supervisor_heartbeat={
            "ts": audit.datetime.now(audit.UTC).isoformat(),
            "bots": [],
        },
        live_broker_state={
            "ready": False,
            "source": "live_broker_rest",
            "broker_snapshot_state": "fresh",
            "open_position_count": 0,
            "ibkr": {
                "ready": False,
                "open_position_count": 0,
                "open_positions": [],
            },
        },
    )

    reconcile = report["safety_gates"]["supervisor_reconcile"]
    assert reconcile["source"] == "supervisor_heartbeat_and_live_broker_state"
    assert reconcile["status"] == "PASS"
    assert reconcile["ready"] is True
    assert reconcile["broker_only_symbols"] == []
    assert reconcile["supervisor_only_symbols"] == []
    assert reconcile["divergent_symbols"] == []
    assert report["summary"]["supervisor_reconcile_ready"] is True
    assert report["runtime"]["tasks"]["stale_artifact_backed_dashboard_durable"] == []
    artifact = report["runtime"]["tasks"]["non_authoritative_task_artifacts"]["ETA-SupervisorBrokerReconcile"]
    assert artifact["status"] == "fresh"
    assert artifact["stale"] is False
    assert artifact["source"] == "supervisor_heartbeat_and_live_broker_state"
    assert artifact["broker_state_source"] == "live_broker_rest"
    assert not any("Refresh supervisor broker reconciliation" in action for action in report["next_actions"])
    assert not any("non-authoritative dashboard watch artifacts" in action for action in report["next_actions"])


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


def test_non_authoritative_cached_dashboard_artifacts_narrow_local_repair_action() -> None:
    tasks = _healthy_tasks()
    for name in (
        "ETA-BrokerStateRefreshHeartbeat",
        "ETA-SupervisorBrokerReconcile",
        "ETA-OperatorQueueHeartbeat",
        "ETA-PaperLiveTransitionCheck",
    ):
        tasks[name] = {"task_name": name, "state": "Missing", "last_task_result": None}

    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=tasks,
        ibgateway_reauth={
            "status": "non_authoritative_gateway_host",
            "reason": "Refusing IBKR Gateway recovery on this host because the VPS is the 24/7 Gateway authority.",
            "gateway_authority": {
                "allowed": False,
                "source": "missing_marker",
                "computer_name": "ETA",
            },
        },
        non_authoritative_task_artifacts=_non_authoritative_task_artifacts(
            supervisor_reconcile_status="stale",
            operator_queue_status="fresh",
            paper_live_transition_status="stale",
        ),
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["dashboard_durable"] is False
    assert report["runtime"]["tasks"]["observed_missing_dashboard_durable"] == [
        "ETA-BrokerStateRefreshHeartbeat",
        "ETA-SupervisorBrokerReconcile",
        "ETA-OperatorQueueHeartbeat",
        "ETA-PaperLiveTransitionCheck",
    ]
    assert report["runtime"]["tasks"]["missing_dashboard_durable"] == ["ETA-BrokerStateRefreshHeartbeat"]
    assert report["runtime"]["tasks"]["artifact_backed_missing_dashboard_durable"] == [
        "ETA-SupervisorBrokerReconcile",
        "ETA-OperatorQueueHeartbeat",
        "ETA-PaperLiveTransitionCheck",
    ]
    assert report["runtime"]["tasks"]["stale_artifact_backed_dashboard_durable"] == [
        "ETA-SupervisorBrokerReconcile",
        "ETA-PaperLiveTransitionCheck",
    ]
    assert report["summary"]["missing_dashboard_durable"] == ["ETA-BrokerStateRefreshHeartbeat"]
    assert report["summary"]["stale_artifact_backed_dashboard_durable"] == [
        "ETA-SupervisorBrokerReconcile",
        "ETA-PaperLiveTransitionCheck",
    ]
    assert any(
        "repair_dashboard_durability_admin.cmd" in action
        and "ETA-BrokerStateRefreshHeartbeat" in action
        and "ETA-OperatorQueueHeartbeat" not in action
        for action in report["next_actions"]
    )
    stale_action = next(
        action
        for action in report["next_actions"]
        if "non-authoritative dashboard watch artifacts" in action
    )
    assert "run_supervisor_broker_reconcile_task.cmd" in stale_action
    assert "on the VPS/Gateway-authoritative host" in stale_action
    assert "run_paper_live_transition_check.cmd" in stale_action


def test_non_authoritative_live_broker_refresh_clears_last_dashboard_gap() -> None:
    tasks = _healthy_tasks()
    for name in (
        "ETA-BrokerStateRefreshHeartbeat",
        "ETA-SupervisorBrokerReconcile",
        "ETA-OperatorQueueHeartbeat",
        "ETA-PaperLiveTransitionCheck",
    ):
        tasks[name] = {"task_name": name, "state": "Missing", "last_task_result": None}

    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=tasks,
        ibgateway_reauth={
            "status": "non_authoritative_gateway_host",
            "reason": "Refusing IBKR Gateway recovery on this host because the VPS is the 24/7 Gateway authority.",
            "gateway_authority": {
                "allowed": False,
                "source": "missing_marker",
                "computer_name": "ETA",
            },
        },
        non_authoritative_task_artifacts=_non_authoritative_task_artifacts(
            broker_state_refresh_status="fresh",
            supervisor_reconcile_status="stale",
            operator_queue_status="fresh",
            paper_live_transition_status="stale",
        ),
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["dashboard_durable"] is True
    assert report["runtime"]["tasks"]["missing_dashboard_durable"] == []
    assert report["runtime"]["tasks"]["artifact_backed_missing_dashboard_durable"] == [
        "ETA-BrokerStateRefreshHeartbeat",
        "ETA-SupervisorBrokerReconcile",
        "ETA-OperatorQueueHeartbeat",
        "ETA-PaperLiveTransitionCheck",
    ]
    assert not any("repair_dashboard_durability_admin.cmd" in action for action in report["next_actions"])
    assert any(
        "run_supervisor_broker_reconcile_task.cmd" in action
        and "run_paper_live_transition_check.cmd" in action
        for action in report["next_actions"]
    )


def test_access_denied_dashboard_tasks_do_not_count_as_missing() -> None:
    report = audit.build_report(
        services=_running_services(),
        ports=_listening_ports(),
        endpoints=_healthy_endpoints(),
        broker_bracket_audit={"summary": {"status": "PASS", "ready_for_prop_dry_run": True}},
        promotion_audit={"summary": {"status": "PASS", "ready_for_live": True}},
        service_config={"fm_status_server": {"matches_expected": True}},
        tasks=_dashboard_tasks_access_denied(),
        ibgateway_reauth={"status": "healthy"},
    )

    assert report["summary"]["status"] == "GREEN_READY_FOR_SOAK"
    assert report["summary"]["dashboard_durable"] is True
    assert report["runtime"]["tasks"]["missing_dashboard_durable"] == []
    assert "ETA-Dashboard-API" in report["runtime"]["tasks"]["access_denied_dashboard_durable"]
    assert "ETA-Proxy-8421" in report["runtime"]["tasks"]["access_denied_dashboard_durable"]
    assert not any("repair_dashboard_durability_admin.cmd" in action for action in report["next_actions"])


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
    assert report["runtime"]["tasks"]["data_pipeline"] == [
        "ETA-SymbolIntelCollector",
        "ETA-IndexFutures-Bar-Refresh",
    ]
    assert report["runtime"]["tasks"]["missing_data_pipeline"] == ["ETA-SymbolIntelCollector"]
    assert any("data-pipeline" in action for action in report["next_actions"])


def test_non_authoritative_cached_data_pipeline_artifact_downgrades_local_missing_task() -> None:
    tasks = _healthy_tasks()
    tasks["ETA-IndexFutures-Bar-Refresh"] = {
        "task_name": "ETA-IndexFutures-Bar-Refresh",
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
        ibgateway_reauth={
            "status": "non_authoritative_gateway_host",
            "reason": "Refusing IBKR Gateway recovery on this host because the VPS is the 24/7 Gateway authority.",
            "gateway_authority": {
                "allowed": False,
                "source": "missing_marker",
                "computer_name": "ETA",
            },
        },
        non_authoritative_task_artifacts=_non_authoritative_task_artifacts(data_pipeline_status="fresh"),
    )

    assert report["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
    assert report["summary"]["runtime_ready"] is True
    assert report["runtime"]["tasks"]["observed_missing_data_pipeline"] == ["ETA-IndexFutures-Bar-Refresh"]
    assert report["runtime"]["tasks"]["missing_data_pipeline"] == []
    assert report["runtime"]["tasks"]["artifact_backed_missing_data_pipeline"] == ["ETA-IndexFutures-Bar-Refresh"]
    assert report["runtime"]["tasks"]["stale_artifact_backed_data_pipeline"] == []
    assert not any("Repair data-pipeline scheduled task lane" in action for action in report["next_actions"])


def test_index_futures_bar_refresh_is_data_pipeline_task() -> None:
    assert "ETA-IndexFutures-Bar-Refresh" in audit.DATA_PIPELINE_TASKS


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


def test_collect_non_authoritative_task_artifacts_uses_snapshot_fallbacks(tmp_path, monkeypatch) -> None:
    paper_live_snapshot = tmp_path / "paper_live_transition_check.json"
    paper_live_snapshot.write_text("{}", encoding="utf-8")
    fm_health_snapshot = tmp_path / "fm_health.json"
    fm_health_snapshot.write_text("{}", encoding="utf-8")
    operator_queue_snapshot = tmp_path / "operator_queue_snapshot.json"
    operator_queue_snapshot.write_text("{}", encoding="utf-8")
    supervisor_reconcile_snapshot = tmp_path / "reconcile_last.json"
    supervisor_reconcile_snapshot.write_text("{}", encoding="utf-8")
    symbol_snapshot = tmp_path / "symbol_intelligence_latest.json"
    symbol_snapshot.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        audit,
        "NON_AUTHORITATIVE_TASK_ARTIFACTS",
        {
            "ETA-SupervisorBrokerReconcile": {
                "max_age_s": 3600,
                "artifacts": (
                    {"name": "supervisor_reconcile", "path": supervisor_reconcile_snapshot},
                ),
            },
            "ETA-OperatorQueueHeartbeat": {
                "max_age_s": 3600,
                "artifacts": (
                    {"name": "operator_queue_snapshot", "path": operator_queue_snapshot},
                ),
            },
            "ETA-PaperLiveTransitionCheck": {
                "max_age_s": 3600,
                "artifacts": (
                    {"name": "paper_live_transition_check", "path": paper_live_snapshot},
                ),
            },
            "ETA-ThreeAI-Sync": {
                "max_age_s": 3600,
                "artifacts": (
                    {"name": "fm_health_snapshot", "path": fm_health_snapshot},
                ),
            },
            "ETA-PaperLive-Supervisor": {
                "max_age_s": 3600,
                "artifacts": (
                    {"name": "paper_live_transition_check", "path": paper_live_snapshot},
                ),
            },
            "ETA-IndexFutures-Bar-Refresh": {
                "max_age_s": 3600,
                "artifacts": (
                    {"name": "index_futures_bar_refresh", "path": tmp_path / "missing.json"},
                    {"name": "symbol_intelligence_snapshot", "path": symbol_snapshot},
                ),
            },
        },
    )

    coverage = audit.collect_non_authoritative_task_artifacts(
        now=audit.datetime.now(audit.UTC),
        live_broker_state={
            "server_ts": audit.datetime.now(audit.UTC).timestamp(),
            "broker_snapshot_state": "fresh",
            "source": "live_broker_rest",
            "ready": False,
        },
    )

    assert coverage["ETA-BrokerStateRefreshHeartbeat"]["status"] == "fresh"
    assert coverage["ETA-SupervisorBrokerReconcile"]["status"] == "fresh"
    assert coverage["ETA-OperatorQueueHeartbeat"]["status"] == "fresh"
    assert coverage["ETA-PaperLiveTransitionCheck"]["status"] == "fresh"
    assert coverage["ETA-ThreeAI-Sync"]["status"] == "fresh"
    assert coverage["ETA-PaperLive-Supervisor"]["status"] == "fresh"
    assert coverage["ETA-IndexFutures-Bar-Refresh"]["covered"] is True
    assert coverage["ETA-IndexFutures-Bar-Refresh"]["source"] == "symbol_intelligence_snapshot"


def test_write_latest_alias_writes_canonical_json(tmp_path, monkeypatch) -> None:
    out_path = tmp_path / "vps_ops_hardening_latest.json"
    monkeypatch.setattr(audit, "DEFAULT_OUT", out_path)
    monkeypatch.setattr(
        audit,
        "collect_live_report",
        lambda: {"summary": {"status": "YELLOW_SAFETY_BLOCKED"}, "next_actions": []},
    )

    rc = audit.main(["--write-latest"])

    assert rc == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["summary"]["status"] == "YELLOW_SAFETY_BLOCKED"
