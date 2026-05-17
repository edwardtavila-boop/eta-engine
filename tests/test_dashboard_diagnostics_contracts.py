from __future__ import annotations

from eta_engine.deploy.scripts.dashboard_diagnostics_contracts import (
    build_dashboard_diagnostics_checks,
)


def test_build_dashboard_diagnostics_checks_accepts_expected_contract_values() -> None:
    payload = build_dashboard_diagnostics_checks(
        card_summary={"dead": 0, "stale": 0},
        roster={"bots": []},
        equity={"series": []},
        readiness={"status": "ready", "error": ""},
        second_brain={"n_episodes": 3, "playbook": {}},
        symbol_intelligence={"contract_ok": True},
        diamond_retune_status={"contract_ok": True},
        daily_loss_killswitch={"status": "clear"},
        live_broker_diagnostics={"ready": True, "broker_snapshot_source": "ibkr_probe_cache"},
        operator_queue={"summary": {}},
        paper_live_transition={"status": "ready_to_launch_paper_live"},
        dashboard_proxy_watchdog={"status": "ok"},
        command_center_watchdog={"status": "healthy"},
        eta_readiness_snapshot={"status": "ready"},
        daily_stop_reset_audit={"status": "ok"},
        vps_ops_hardening={"status": "healthy"},
        session_gate_signal_audit={"status": "ok"},
        required_data={"auth_session"},
        daily_stop_reset_audit_statuses={"ok", "warn", "missing"},
        vps_ops_hardening_statuses={"healthy", "warn", "unknown"},
    )

    assert payload["api_contract"] is True
    assert payload["card_contract"] is True
    assert payload["operator_queue_contract"] is True
    assert payload["paper_live_transition_contract"] is True
    assert payload["command_center_watchdog_contract"] is True
    assert payload["auth_contract"] is True


def test_build_dashboard_diagnostics_checks_flags_missing_auth_and_bad_statuses() -> None:
    payload = build_dashboard_diagnostics_checks(
        card_summary={"dead": 1, "stale": 0},
        roster={"bots": "bad"},
        equity={},
        readiness={"status": "blocked", "error": "missing"},
        second_brain={"n_episodes": 0, "playbook": []},
        symbol_intelligence={"contract_ok": False},
        diamond_retune_status={"contract_ok": False},
        daily_loss_killswitch={"status": "broken"},
        live_broker_diagnostics={"ready": True},
        operator_queue={},
        paper_live_transition={},
        dashboard_proxy_watchdog={"status": "broken"},
        command_center_watchdog={"status": "broken"},
        eta_readiness_snapshot={"status": "broken"},
        daily_stop_reset_audit={"status": "broken"},
        vps_ops_hardening={"status": "broken"},
        session_gate_signal_audit={"status": "broken"},
        required_data=set(),
        daily_stop_reset_audit_statuses={"ok", "warn", "missing"},
        vps_ops_hardening_statuses={"healthy", "warn", "unknown"},
    )

    assert payload["card_contract"] is False
    assert payload["bot_fleet_contract"] is False
    assert payload["equity_contract"] is False
    assert payload["bot_strategy_readiness_contract"] is False
    assert payload["second_brain_contract"] is False
    assert payload["symbol_intelligence_contract"] is False
    assert payload["diamond_retune_status_contract"] is False
    assert payload["daily_loss_killswitch_contract"] is False
    assert payload["live_broker_state_contract"] is False
    assert payload["operator_queue_contract"] is False
    assert payload["paper_live_transition_contract"] is False
    assert payload["dashboard_proxy_watchdog_contract"] is False
    assert payload["command_center_watchdog_contract"] is False
    assert payload["eta_readiness_snapshot_contract"] is False
    assert payload["daily_stop_reset_audit_contract"] is False
    assert payload["vps_ops_hardening_contract"] is False
    assert payload["hardening_contract"] is False
    assert payload["session_gate_signal_audit_contract"] is False
    assert payload["auth_contract"] is False
