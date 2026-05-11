"""Tests for read-only broker bracket/OCO coverage audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eta_engine.scripts import broker_bracket_audit as audit


def test_bracket_audit_ready_when_flat(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(fleet={"summary": {"broker_open_position_count": 0}})

    assert report["summary"] == "READY_NO_OPEN_EXPOSURE"
    assert report["ready_for_prop_dry_run"] is True
    assert report["operator_action_required"] is False
    assert report["operator_actions"] == []


def test_bracket_audit_blocks_when_fleet_position_truth_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(fleet={})

    assert report["summary"] == "BLOCKED_FLEET_TRUTH_UNAVAILABLE"
    assert report["fleet_truth_present"] is False
    assert report["ready_for_prop_dry_run"] is False
    assert report["operator_action_required"] is True
    assert report["operator_actions"][0]["id"] == "restore_bot_fleet_position_truth"
    assert report["operator_actions"][0]["order_action"] is False
    assert "/api/bot-fleet" in report["next_action"]


def test_bracket_audit_blocks_unbracketed_open_exposure(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(
        fleet={
            "summary": {
                "broker_open_position_count": 2,
                "broker_bracket_count": 0,
                "supervisor_local_position_count": 0,
            },
        },
    )

    assert report["summary"] == "BLOCKED_UNBRACKETED_EXPOSURE"
    assert report["ready_for_prop_dry_run"] is False
    assert report["operator_action_required"] is True
    assert [action["id"] for action in report["operator_actions"]] == [
        "verify_manual_broker_oco",
        "flatten_unprotected_paper_exposure",
    ]


def test_bracket_audit_prefers_target_exit_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(
        fleet={
            "summary": {
                "broker_open_position_count": 0,
                "broker_bracket_count": 0,
                "supervisor_local_position_count": 0,
            },
            "target_exit_summary": {
                "broker_open_position_count": 2,
                "broker_bracket_count": 0,
                "supervisor_local_position_count": 3,
            },
        },
    )

    assert report["summary"] == "BLOCKED_UNBRACKETED_EXPOSURE"
    assert report["position_summary"]["broker_open_position_count"] == 2
    assert report["position_summary"]["supervisor_local_position_count"] == 3


def test_bracket_audit_preserves_bracket_required_counts(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(
        fleet={
            "target_exit_summary": {
                "status": "missing_brackets",
                "broker_open_position_count": 2,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 0,
                "missing_bracket_count": 1,
                "supervisor_local_position_count": 4,
                "stale_position_status": "require_ack",
            },
        },
    )

    assert report["summary"] == "BLOCKED_UNBRACKETED_EXPOSURE"
    assert report["position_summary"]["broker_open_position_count"] == 2
    assert report["position_summary"]["broker_bracket_required_position_count"] == 1
    assert report["position_summary"]["missing_bracket_count"] == 1
    assert report["target_exit_status"] == "missing_brackets"
    assert report["stale_position_status"] == "require_ack"
    assert "1 broker bracket-required position" in report["next_action"]
    assert "manual broker OCO" in report["next_action"]


def test_bracket_audit_names_unprotected_broker_position(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(
        fleet={
            "target_exit_summary": {
                "status": "missing_brackets",
                "broker_open_position_count": 2,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 0,
                "missing_bracket_count": 1,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "position_exposure": {
                    "open_positions": [
                        {
                            "venue": "ibkr",
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "avg_entry_price": 29340.0,
                            "current_price": 29335.0,
                            "unrealized_pct": -0.00017,
                            "market_value": 176010.07,
                            "unrealized_pnl": -33.79,
                            "broker_bracket_required": True,
                        },
                        {
                            "venue": "alpaca",
                            "symbol": "ETHUSD",
                            "side": "short",
                            "qty": 0.25,
                            "broker_bracket_required": False,
                        },
                    ],
                },
            },
        },
    )

    assert report["position_summary"]["unprotected_symbols"] == ["MNQM6"]
    assert report["primary_unprotected_position"]["symbol"] == "MNQM6"
    assert report["primary_unprotected_position"]["venue"] == "ibkr"
    assert report["primary_unprotected_position"]["sec_type"] == "FUT"
    assert report["primary_unprotected_position"]["avg_entry_price"] == 29340.0
    assert report["primary_unprotected_position"]["current_price"] == 29335.0
    assert report["primary_unprotected_position"]["unrealized_pct"] == -0.00017
    assert report["unprotected_positions"][0]["broker_bracket_required"] is True
    assert report["unprotected_positions"][0]["avg_entry_price"] == 29340.0
    assert report["unprotected_positions"][0]["current_price"] == 29335.0
    assert report["operator_action"] == report["next_action"]
    assert report["operator_actions"][0]["symbol"] == "MNQM6"
    assert report["operator_actions"][0]["order_action"] is False
    assert report["operator_actions"][1]["order_action"] is True
    assert "MNQM6 IBKR FUT missing broker-native OCO" in report["next_action"]
    assert ".;" not in report["next_action"]


def test_bracket_audit_normalizes_ibkr_futures_average_cost_to_points(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(
        fleet={
            "target_exit_summary": {
                "status": "missing_brackets",
                "broker_open_position_count": 1,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 0,
                "missing_bracket_count": 1,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "position_exposure": {
                    "open_positions": [
                        {
                            "venue": "ibkr",
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "averageCost": 58681.28666665,
                            "currentPrice": 29335.01171875,
                            "broker_bracket_required": True,
                        },
                    ],
                },
            },
        },
    )

    assert round(report["primary_unprotected_position"]["avg_entry_price"], 2) == 29340.64
    assert report["primary_unprotected_position"]["current_price"] == 29335.01171875


def test_bracket_audit_accepts_current_manual_oco_ack(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(
        fleet={
            "target_exit_summary": {
                "status": "missing_brackets",
                "broker_open_position_count": 2,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 0,
                "missing_bracket_count": 1,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "position_exposure": {
                    "open_positions": [
                        {
                            "venue": "ibkr",
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "broker_bracket_required": True,
                        },
                    ],
                },
            },
        },
        manual_ack={
            "schema_version": 1,
            "kind": "eta_broker_bracket_manual_oco_ack",
            "symbol": "MNQM6",
            "venue": "ibkr",
            "verified": True,
            "operator": "edward",
            "verified_at_utc": datetime.now(UTC).isoformat(),
            "expires_at_utc": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "note": "verified in TWS",
        },
    )

    assert report["summary"] == "READY_OPEN_EXPOSURE_MANUAL_OCO_VERIFIED"
    assert report["ready_for_prop_dry_run"] is True
    assert report["operator_action_required"] is False
    assert report["position_summary"]["missing_bracket_count"] == 0
    assert report["position_summary"]["manual_oco_verified_count"] == 1
    assert report["position_summary"]["manual_oco_verified_symbols"] == ["MNQM6"]
    assert report["manual_oco_verified_positions"][0]["symbol"] == "MNQM6"
    assert report["unprotected_positions"] == []
    assert report["operator_actions"] == []


def test_bracket_audit_rejects_expired_manual_oco_ack(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(
        fleet={
            "target_exit_summary": {
                "status": "missing_brackets",
                "broker_open_position_count": 1,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 0,
                "missing_bracket_count": 1,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "position_exposure": {
                    "open_positions": [
                        {
                            "venue": "ibkr",
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "broker_bracket_required": True,
                        },
                    ],
                },
            },
        },
        manual_ack={
            "schema_version": 1,
            "kind": "eta_broker_bracket_manual_oco_ack",
            "symbol": "MNQM6",
            "venue": "ibkr",
            "verified": True,
            "operator": "edward",
            "verified_at_utc": "2000-01-01T00:00:00+00:00",
            "expires_at_utc": "2000-01-02T00:00:00+00:00",
        },
    )

    assert report["summary"] == "BLOCKED_UNBRACKETED_EXPOSURE"
    assert report["ready_for_prop_dry_run"] is False
    assert report["position_summary"]["missing_bracket_count"] == 1
    assert report["position_summary"]["unprotected_symbols"] == ["MNQM6"]
    assert report["manual_oco_verified_positions"] == []
    assert report["primary_unprotected_position"]["symbol"] == "MNQM6"


def test_bracket_audit_derives_summary_from_bots(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )

    report = audit.build_bracket_audit(
        fleet={
            "bots": [
                {"id": "volume_profile_mnq", "open_positions": 1, "broker_bracket": True},
                {"id": "mym_sweep_reclaim", "open_positions": 1, "broker_bracket": True},
            ],
        },
    )

    assert report["summary"] == "READY_OPEN_EXPOSURE_BRACKETED"
    assert report["position_summary"]["broker_bracket_count"] == 2
