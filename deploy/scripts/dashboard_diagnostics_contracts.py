"""Shared diagnostics contract helpers for the ETA dashboard."""

from __future__ import annotations

from typing import Any


def build_dashboard_diagnostics_checks(
    *,
    card_summary: dict[str, Any],
    roster: dict[str, Any],
    equity: dict[str, Any],
    readiness: dict[str, Any],
    second_brain: dict[str, Any],
    symbol_intelligence: dict[str, Any],
    diamond_retune_status: dict[str, Any],
    daily_loss_killswitch: dict[str, Any],
    live_broker_diagnostics: dict[str, Any],
    operator_queue: dict[str, Any],
    paper_live_transition: dict[str, Any],
    dashboard_proxy_watchdog: dict[str, Any],
    command_center_watchdog: dict[str, Any],
    eta_readiness_snapshot: dict[str, Any],
    daily_stop_reset_audit: dict[str, Any],
    vps_ops_hardening: dict[str, Any],
    session_gate_signal_audit: dict[str, Any],
    required_data: set[str] | frozenset[str] | tuple[str, ...],
    daily_stop_reset_audit_statuses: set[str] | frozenset[str],
    vps_ops_hardening_statuses: set[str] | frozenset[str],
) -> dict[str, bool]:
    """Build the diagnostics `checks` payload."""

    return {
        "api_contract": True,
        "card_contract": int(card_summary.get("dead") or 0) == 0 and int(card_summary.get("stale") or 0) == 0,
        "bot_fleet_contract": isinstance(roster.get("bots"), list),
        "equity_contract": "series" in equity,
        "bot_strategy_readiness_contract": readiness.get("status") == "ready" and not readiness.get("error"),
        "second_brain_contract": isinstance(second_brain, dict)
        and "n_episodes" in second_brain
        and isinstance(second_brain.get("playbook"), dict),
        "symbol_intelligence_contract": bool(symbol_intelligence.get("contract_ok")),
        "diamond_retune_status_contract": bool(diamond_retune_status.get("contract_ok")),
        "daily_loss_killswitch_contract": daily_loss_killswitch.get("status")
        in {"clear", "tripped", "disabled", "unknown"},
        "live_broker_state_contract": isinstance(live_broker_diagnostics, dict)
        and "ready" in live_broker_diagnostics
        and "broker_snapshot_source" in live_broker_diagnostics,
        "operator_queue_contract": isinstance(operator_queue, dict) and "summary" in operator_queue,
        "paper_live_transition_contract": isinstance(paper_live_transition, dict)
        and "status" in paper_live_transition,
        "dashboard_proxy_watchdog_contract": dashboard_proxy_watchdog.get("status")
        in {
            "ok",
            "missing",
            "stale",
            "probe_ok_watchdog_stale",
            "failed",
            "degraded",
            "unknown",
        },
        "command_center_watchdog_contract": command_center_watchdog.get("status")
        in {
            "access_denied",
            "healthy",
            "missing_receipt",
            "missing_watchdog",
            "stale_receipt",
            "stale_service",
            "service_unreachable",
            "upstream_failure",
            "dashboard_task_contract_drift",
            "local_dependency_gap",
            "service_dependency_gap",
            "public_operator_drift",
            "public_tunnel_service_drift",
            "public_tunnel_token_rejected",
            "repair_prompted",
            "repair_attempted",
            "contract_failure",
            "secret_surface",
            "unknown",
        },
        "eta_readiness_snapshot_contract": eta_readiness_snapshot.get("status")
        in {
            "ready",
            "blocked",
            "missing_receipt",
            "stale_receipt",
            "unknown",
        },
        "daily_stop_reset_audit_contract": daily_stop_reset_audit.get("status")
        in daily_stop_reset_audit_statuses,
        "vps_ops_hardening_contract": vps_ops_hardening.get("status") in vps_ops_hardening_statuses,
        "hardening_contract": vps_ops_hardening.get("status") in vps_ops_hardening_statuses,
        "session_gate_signal_audit_contract": session_gate_signal_audit.get("status")
        in {"ok", "warn", "missing", "unreadable"},
        "auth_contract": "auth_session" in required_data,
    }
