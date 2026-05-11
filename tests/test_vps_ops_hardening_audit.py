from __future__ import annotations

from eta_engine.scripts import vps_ops_hardening_audit as audit


def _running_services() -> dict[str, dict[str, object]]:
    return {
        name: {"name": name, "status": "Running", "start_type": "Automatic"}
        for name in audit.CRITICAL_SERVICES
    }


def _listening_ports() -> dict[int, dict[str, object]]:
    return {
        8420: {"port": 8420, "listening": True, "owners": ["FirmCommandCenter"]},
        8422: {"port": 8422, "listening": True, "owners": ["FmStatusServer"]},
    }


def _healthy_endpoints() -> dict[str, dict[str, object]]:
    return {
        "local_fm_status": {"ok": True, "status_code": 200},
        "local_command_center_master": {"ok": True, "status_code": 200},
        "public_ops_bot_fleet": {"ok": True, "status_code": 200},
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
                "expected_executable": r"C:\Python314\python.exe",
                "installed_executable": r"C:\OldPython\python.exe",
            }
        },
    )

    assert report["summary"]["status"] == "YELLOW_RESTART_REQUIRED"
    assert report["summary"]["runtime_ready"] is True
    assert report["summary"]["promotion_allowed"] is False
    assert any("elevated" in action.lower() for action in report["next_actions"])


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
