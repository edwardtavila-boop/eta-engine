"""Tests for read-only broker bracket/OCO coverage audit."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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


def test_bracket_audit_fetch_json_retries_transient_live_failure(monkeypatch) -> None:
    calls = {"count": 0}

    class _Response:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        @staticmethod
        def read() -> bytes:
            return b'{"summary":{"broker_open_position_count":0}}'

    def _flaky_urlopen(_request, *, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise audit.urllib.error.URLError("temporary timeout")
        return _Response()

    monkeypatch.setattr(audit.urllib.request, "urlopen", _flaky_urlopen)

    payload = audit._fetch_json("https://ops.example.invalid/api/bot-fleet")  # noqa: SLF001

    assert payload == {"summary": {"broker_open_position_count": 0}}
    assert calls["count"] == 2


def test_bracket_audit_loads_local_bot_fleet_fallback_when_primary_fetch_empty(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []

    def _fake_fetch(url: str, timeout_s: float = 10.0, attempts: int = 2) -> dict[str, object]:
        calls.append((url, timeout_s))
        if url == audit.DEFAULT_FLEET_URL:
            return {}
        return {"target_exit_summary": {"broker_open_position_count": 0}}

    monkeypatch.setattr(audit, "_fetch_json", _fake_fetch)

    payload = audit.load_fleet_payload()

    assert payload == {"target_exit_summary": {"broker_open_position_count": 0}}
    assert calls == [
        (audit.DEFAULT_FLEET_URL, 10.0),
        ("http://127.0.0.1:8421/api/bot-fleet", 20.0),
    ]


def test_bracket_audit_prefers_local_broker_truth_over_public_paper_watch(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []

    public_payload = {
        "target_exit_summary": {
            "broker_open_position_count": 0,
            "supervisor_local_position_count": 10,
            "status": "paper_watching",
        },
    }
    local_payload = {
        "target_exit_summary": {
            "broker_open_position_count": 4,
            "broker_bracket_required_position_count": 4,
            "broker_bracket_count": 4,
            "missing_bracket_count": 0,
            "supervisor_local_position_count": 6,
            "status": "watching",
        },
        "live_broker_state": {
            "ibkr": {
                "open_positions": [{"symbol": "MNQM6", "secType": "FUT", "position": 1}],
                "open_orders": [{"symbol": "MNQM6", "action": "SELL", "order_type": "LMT", "qty": 1}],
            },
        },
    }

    def _fake_fetch(url: str, timeout_s: float = 10.0, attempts: int = 2) -> dict[str, object]:
        calls.append((url, timeout_s))
        if url == audit.DEFAULT_FLEET_URL:
            return public_payload
        return local_payload

    monkeypatch.setattr(audit, "_fetch_json", _fake_fetch)

    payload = audit.load_fleet_payload()

    assert payload is local_payload
    assert calls == [
        (audit.DEFAULT_FLEET_URL, 10.0),
        ("http://127.0.0.1:8421/api/bot-fleet", 5.0),
    ]


def test_bracket_audit_operator_action_uses_singular_for_unknown_exposure() -> None:
    report = audit.build_bracket_audit(
        fleet={
            "target_exit_summary": {
                "broker_open_position_count": 1,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 0,
                "missing_bracket_count": 1,
                "supervisor_local_position_count": 0,
            },
        },
    )

    assert (
        report["operator_actions"][0]["detail"]
        == "Confirm current broker exposure has broker-native TP/SL OCO attached outside ETA."
    )


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


def test_bracket_audit_operator_actions_list_all_unprotected_symbols(monkeypatch) -> None:
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
                "broker_bracket_required_position_count": 2,
                "broker_bracket_count": 0,
                "missing_bracket_count": 2,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "position_exposure": {
                    "open_positions": [
                        {
                            "venue": "ibkr",
                            "symbol": "MCLM6",
                            "secType": "FUT",
                            "position": -1,
                            "broker_bracket_required": True,
                        },
                        {
                            "venue": "ibkr",
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 13,
                            "broker_bracket_required": True,
                        },
                    ],
                },
            },
        },
    )

    assert report["operator_actions"][0]["symbols"] == ["MCLM6", "MNQM6"]
    assert report["operator_actions"][1]["symbols"] == ["MCLM6", "MNQM6"]
    assert "MCLM6, MNQM6" in report["operator_actions"][0]["detail"]
    assert "MCLM6, MNQM6" in report["operator_actions"][1]["detail"]


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


def test_bracket_audit_accepts_ibkr_open_order_oco_evidence(monkeypatch) -> None:
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
                "ibkr": {
                    "open_positions": [
                        {
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "broker_bracket_required": True,
                        },
                    ],
                    "open_orders": [
                        {
                            "symbol": "MNQM6",
                            "action": "SELL",
                            "order_type": "LMT",
                            "qty": 3,
                            "parent_id": 1001,
                            "status": "Submitted",
                        },
                        {
                            "symbol": "MNQM6",
                            "action": "SELL",
                            "order_type": "STP",
                            "qty": 3,
                            "parent_id": 1001,
                            "status": "Submitted",
                        },
                    ],
                },
            },
        },
    )

    assert report["summary"] == "READY_OPEN_EXPOSURE_BRACKETED"
    assert report["ready_for_prop_dry_run"] is True
    assert report["operator_action_required"] is False
    assert report["position_summary"]["missing_bracket_count"] == 0
    assert report["position_summary"]["broker_oco_verified_count"] == 1
    assert report["position_summary"]["broker_oco_verified_symbols"] == ["MNQM6"]
    assert report["broker_oco_verified_positions"][0]["coverage_status"] == "broker_oco_verified"
    assert report["unprotected_positions"] == []
    assert report["operator_actions"] == []


def test_bracket_audit_does_not_block_bracketed_broker_exposure_on_paper_watches(monkeypatch) -> None:
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
                "status": "watching",
                "broker_open_position_count": 4,
                "broker_bracket_required_position_count": 4,
                "broker_bracket_count": 4,
                "missing_bracket_count": 0,
                "supervisor_local_position_count": 6,
                "stale_position_status": "force_flatten_due",
            },
        },
    )

    assert report["summary"] == "READY_OPEN_EXPOSURE_BRACKETED"
    assert report["ready_for_prop_dry_run"] is True
    assert report["operator_action_required"] is False
    assert report["position_summary"]["broker_open_position_count"] == 4
    assert report["position_summary"]["broker_bracket_count"] == 4
    assert report["position_summary"]["missing_bracket_count"] == 0
    assert report["position_summary"]["supervisor_local_position_count"] == 6
    assert report["stale_position_status"] == "force_flatten_due"
    assert report["operator_actions"] == []


def test_bracket_audit_blocks_active_open_orders_for_flat_symbols(monkeypatch) -> None:
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
                "status": "watching",
                "broker_open_position_count": 2,
                "broker_bracket_required_position_count": 2,
                "broker_bracket_count": 2,
                "missing_bracket_count": 0,
                "supervisor_local_position_count": 2,
            },
            "live_broker_state": {
                "ibkr": {
                    "open_positions": [
                        {"symbol": "MNQM6", "secType": "FUT", "position": 1, "broker_bracket_required": True},
                        {"symbol": "MBTK6", "secType": "FUT", "position": 1, "broker_bracket_required": True},
                    ],
                    "open_orders": [
                        {"symbol": "MNQM6", "action": "SELL", "order_type": "LMT", "qty": 1, "status": "Submitted"},
                        {
                            "symbol": "MCLM6",
                            "action": "BUY",
                            "order_type": "LMT",
                            "qty": 1,
                            "status": "Submitted",
                            "order_id": 1404,
                            "client_id": 188,
                        },
                        {
                            "symbol": "MYMM6",
                            "action": "SELL",
                            "order_type": "STP",
                            "qty": 1,
                            "status": "PreSubmitted",
                            "order_id": 1405,
                            "client_id": 188,
                        },
                    ],
                },
            },
        },
    )

    assert report["summary"] == "BLOCKED_STALE_FLAT_OPEN_ORDERS"
    assert report["ready_for_prop_dry_run"] is False
    assert report["operator_action_required"] is True
    assert report["position_summary"]["stale_flat_open_order_count"] == 2
    assert report["position_summary"]["stale_flat_open_order_symbols"] == ["MCLM6", "MYMM6"]
    assert [row["symbol"] for row in report["stale_flat_open_orders"]] == ["MCLM6", "MYMM6"]
    action = report["operator_actions"][0]
    assert action["id"] == "cancel_stale_flat_open_orders"
    assert action["order_action"] is True
    assert action["order_ids"] == [1404, 1405]
    assert action["owner_client_ids"] == [188]
    assert action["confirm_requires_operator_approval"] is True
    assert action["confirm_requires_matching_owner_client_id"] is True
    assert action["no_global_cancel"] is True
    assert "--client-id 9031 --symbols MCLM6,MYMM6" in action["dry_run_command"]
    assert "--client-id 188 --symbols MCLM6,MYMM6 --confirm" in action["confirm_command_template"]
    assert "Owner IBKR clientId(s): 188" in action["detail"]
    assert "MCLM6, MYMM6" in report["next_action"]


def test_bracket_audit_clears_stale_flat_orders_when_live_socket_has_none(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )
    monkeypatch.setattr(
        audit,
        "_validate_stale_flat_open_orders_live",
        lambda stale_orders, **_kwargs: (
            [],
            {
                "status": "live_socket_no_open_orders",
                "live_open_trade_count": 0,
                "live_open_order_count": 0,
                "input_stale_flat_open_order_count": len(stale_orders),
                "validated_stale_flat_open_order_count": 0,
            },
        ),
    )

    report = audit.build_bracket_audit(
        fleet={
            "target_exit_summary": {
                "status": "watching",
                "broker_open_position_count": 1,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 1,
                "missing_bracket_count": 0,
                "supervisor_local_position_count": 1,
            },
            "live_broker_state": {
                "ibkr": {
                    "open_positions": [
                        {"symbol": "MBTK6", "secType": "FUT", "position": 1, "broker_bracket_required": True},
                    ],
                    "open_orders": [
                        {"symbol": "MYMM6", "action": "SELL", "order_type": "LMT", "qty": 1, "status": "Submitted"},
                    ],
                },
            },
        },
        validate_live_stale_orders=True,
    )

    assert report["summary"] == "READY_OPEN_EXPOSURE_BRACKETED"
    assert report["ready_for_prop_dry_run"] is True
    assert report["stale_flat_open_orders"] == []
    assert report["stale_flat_open_order_validation"]["status"] == "live_socket_no_open_orders"


def test_bracket_audit_surfaces_pending_cancel_stale_orders_explicitly(monkeypatch) -> None:
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
                "status": "watching",
                "broker_open_position_count": 1,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 1,
                "missing_bracket_count": 0,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "ibkr": {
                    "open_positions": [
                        {"symbol": "MBTK6", "secType": "FUT", "position": 1, "broker_bracket_required": True},
                    ],
                    "open_orders": [
                        {
                            "symbol": "MCLN6",
                            "action": "SELL",
                            "status": "PendingCancel",
                            "order_id": 1410,
                            "perm_id": 581506503,
                            "parent_id": 1409,
                            "client_id": 188,
                            "oca_group": "581506502",
                        },
                    ],
                },
            },
        },
        validate_live_stale_orders=False,
    )

    assert report["summary"] == "BLOCKED_STALE_FLAT_OPEN_ORDERS"
    assert "Wait for or clear 1 stale PendingCancel broker order(s)" in report["next_action"]
    assert report["operator_actions"][0]["label"] == "Clear pending stale flat-symbol orders"
    assert "already show PendingCancel with IBKR" in report["operator_actions"][0]["detail"]


def test_bracket_audit_mixed_pending_cancel_and_active_stale_orders_report_total_count(monkeypatch) -> None:
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
                "status": "watching",
                "broker_open_position_count": 1,
                "broker_bracket_required_position_count": 1,
                "broker_bracket_count": 1,
                "missing_bracket_count": 0,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "ibkr": {
                    "open_positions": [
                        {"symbol": "MBTK6", "secType": "FUT", "position": 1, "broker_bracket_required": True},
                    ],
                    "open_orders": [
                        {
                            "symbol": "MCLN6",
                            "action": "SELL",
                            "status": "PendingCancel",
                            "order_id": 1410,
                            "perm_id": 581506503,
                            "parent_id": 1409,
                            "client_id": 188,
                            "oca_group": "581506502",
                        },
                        {
                            "symbol": "NQM6",
                            "action": "BUY",
                            "status": "Submitted",
                            "order_id": 1411,
                            "perm_id": 581506504,
                            "parent_id": 1410,
                            "client_id": 188,
                            "oca_group": "581506503",
                        },
                    ],
                },
            },
        },
        validate_live_stale_orders=False,
    )

    assert report["summary"] == "BLOCKED_STALE_FLAT_OPEN_ORDERS"
    assert "Wait for or clear 2 stale broker order(s) for MCLN6, NQM6" in report["next_action"]
    assert "1 already show PendingCancel with IBKR and 1 still have no matching broker open position" in report[
        "next_action"
    ]
    assert report["operator_actions"][0]["label"] == "Clear pending stale flat-symbol orders"
    assert "1 additional stale order remain active with no matching broker position" in report["operator_actions"][0][
        "detail"
    ]


def test_live_stale_order_validation_uses_configured_timeout(monkeypatch) -> None:
    seen: dict[str, float] = {}

    class _FakeIB:
        def connect(self, _host: str, _port: int, *, clientId: int, timeout: float) -> None:
            seen["client_id"] = float(clientId)
            seen["timeout"] = timeout

        @staticmethod
        def reqAllOpenOrders() -> None:
            return None

        @staticmethod
        def sleep(_seconds: float) -> None:
            return None

        @staticmethod
        def openTrades() -> list[object]:
            return []

        @staticmethod
        def openOrders() -> list[object]:
            return []

        @staticmethod
        def disconnect() -> None:
            return None

    monkeypatch.setenv("ETA_BROKER_BRACKET_AUDIT_CONNECT_TIMEOUT_S", "45")
    monkeypatch.setattr(audit, "_ensure_main_thread_event_loop", lambda: None)
    monkeypatch.setitem(sys.modules, "ib_insync", SimpleNamespace(IB=_FakeIB))

    validated, details = audit._validate_stale_flat_open_orders_live([{"symbol": "MBTK6"}])  # noqa: SLF001

    assert validated == []
    assert details["status"] == "live_socket_no_open_orders"
    assert details["client_id_used"] in details["attempted_client_ids"]
    assert seen["timeout"] == 45.0


def test_live_stale_order_validation_retries_when_client_id_is_already_in_use(monkeypatch) -> None:
    seen: list[int] = []

    class _FakeIB:
        def connect(self, _host: str, _port: int, *, clientId: int, timeout: float) -> None:
            seen.append(clientId)
            if len(seen) == 1:
                raise RuntimeError("client id is already in use")

        @staticmethod
        def reqAllOpenOrders() -> None:
            return None

        @staticmethod
        def sleep(_seconds: float) -> None:
            return None

        @staticmethod
        def openTrades() -> list[object]:
            return []

        @staticmethod
        def openOrders() -> list[object]:
            return []

        @staticmethod
        def disconnect() -> None:
            return None

    monkeypatch.setattr(audit, "_ensure_main_thread_event_loop", lambda: None)
    monkeypatch.setitem(sys.modules, "ib_insync", SimpleNamespace(IB=_FakeIB))

    validated, details = audit._validate_stale_flat_open_orders_live([{"symbol": "MBTK6"}], client_id=9034)  # noqa: SLF001

    assert validated == []
    assert details["status"] == "live_socket_no_open_orders"
    assert seen[0] == 9034
    assert details["attempted_client_ids"][:2] == seen
    assert details["client_id_used"] == seen[1]
    assert details["client_id_used"] != 9034


def test_live_stale_order_validation_reconciles_partial_symbol_matches_by_order_id(monkeypatch) -> None:
    class _FakeIB:
        def connect(self, _host: str, _port: int, *, clientId: int, timeout: float) -> None:
            return None

        @staticmethod
        def reqAllOpenOrders() -> None:
            return None

        @staticmethod
        def sleep(_seconds: float) -> None:
            return None

        @staticmethod
        def openTrades() -> list[object]:
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(symbol="MCL", localSymbol="MCLN6"),
                    order=SimpleNamespace(orderId=1410, permId=581506503),
                    orderStatus=SimpleNamespace(status="Submitted"),
                ),
            ]

        @staticmethod
        def openOrders() -> list[object]:
            return [SimpleNamespace(orderId=1410, permId=581506503)]

        @staticmethod
        def disconnect() -> None:
            return None

    monkeypatch.setattr(audit, "_ensure_main_thread_event_loop", lambda: None)
    monkeypatch.setitem(sys.modules, "ib_insync", SimpleNamespace(IB=_FakeIB))

    validated, details = audit._validate_stale_flat_open_orders_live(  # noqa: SLF001
        [
            {"symbol": "MCLN6", "order_id": 1410, "perm_id": 581506503},
            {"symbol": "MCLN6", "order_id": 1411, "perm_id": 581506504},
        ],
    )

    assert [order["order_id"] for order in validated] == [1410]
    assert details["status"] == "live_socket_validated"
    assert details["validated_stale_flat_open_order_count"] == 1
    assert details["live_open_trade_count"] == 1
    assert details["live_open_order_count"] == 1


def test_bracket_audit_keeps_incomplete_ibkr_open_order_coverage_blocked(monkeypatch) -> None:
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
                "ibkr": {
                    "open_positions": [
                        {
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 3,
                            "broker_bracket_required": True,
                        },
                    ],
                    "open_orders": [
                        {
                            "symbol": "MNQM6",
                            "action": "SELL",
                            "order_type": "STP",
                            "qty": 3,
                            "parent_id": 1001,
                            "status": "Submitted",
                        },
                    ],
                },
            },
        },
    )

    assert report["summary"] == "BLOCKED_UNBRACKETED_EXPOSURE"
    assert report["ready_for_prop_dry_run"] is False
    assert report["position_summary"]["missing_bracket_count"] == 1
    assert report["position_summary"]["broker_oco_verified_count"] == 0
    assert report["position_summary"]["unprotected_symbols"] == ["MNQM6"]
    assert report["primary_unprotected_position"]["coverage_status"] == "requires_manual_oco_verification"


def test_bracket_audit_accepts_current_manual_oco_ack_ledger(monkeypatch) -> None:
    monkeypatch.setattr(
        audit,
        "_adapter_support",
        lambda: {
            "ibkr_futures_server_oco": True,
            "alpaca_equity_server_bracket": True,
            "tradovate_order_payload_brackets": True,
        },
    )
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    report = audit.build_bracket_audit(
        fleet={
            "target_exit_summary": {
                "status": "missing_brackets",
                "broker_open_position_count": 2,
                "broker_bracket_required_position_count": 2,
                "broker_bracket_count": 0,
                "missing_bracket_count": 2,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "position_exposure": {
                    "open_positions": [
                        {
                            "venue": "ibkr",
                            "symbol": "MCLM6",
                            "secType": "FUT",
                            "position": -1,
                            "broker_bracket_required": True,
                        },
                        {
                            "venue": "ibkr",
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 13,
                            "broker_bracket_required": True,
                        },
                    ],
                },
            },
        },
        manual_ack={
            "schema_version": 2,
            "kind": "eta_broker_bracket_manual_oco_ack_ledger",
            "acks": [
                {
                    "symbol": "MCLM6",
                    "venue": "ibkr",
                    "verified": True,
                    "operator": "edward",
                    "verified_at_utc": datetime.now(UTC).isoformat(),
                    "expires_at_utc": expires_at,
                    "note": "verified in TWS",
                },
                {
                    "symbol": "MNQM6",
                    "venue": "ibkr",
                    "verified": True,
                    "operator": "edward",
                    "verified_at_utc": datetime.now(UTC).isoformat(),
                    "expires_at_utc": expires_at,
                    "note": "verified in TWS",
                },
            ],
        },
    )

    assert report["summary"] == "READY_OPEN_EXPOSURE_MANUAL_OCO_VERIFIED"
    assert report["ready_for_prop_dry_run"] is True
    assert report["position_summary"]["missing_bracket_count"] == 0
    assert report["position_summary"]["manual_oco_verified_count"] == 2
    assert report["position_summary"]["manual_oco_verified_symbols"] == ["MCLM6", "MNQM6"]
    assert report["manual_oco_ack"]["ack_count"] == 2
    assert report["manual_oco_ack"]["symbols"] == ["MCLM6", "MNQM6"]
    assert report["unprotected_positions"] == []


def test_bracket_audit_keeps_unverified_ledger_symbols_blocked(monkeypatch) -> None:
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
                "broker_bracket_required_position_count": 2,
                "broker_bracket_count": 0,
                "missing_bracket_count": 2,
                "supervisor_local_position_count": 0,
            },
            "live_broker_state": {
                "position_exposure": {
                    "open_positions": [
                        {
                            "venue": "ibkr",
                            "symbol": "MCLM6",
                            "secType": "FUT",
                            "position": -1,
                            "broker_bracket_required": True,
                        },
                        {
                            "venue": "ibkr",
                            "symbol": "MNQM6",
                            "secType": "FUT",
                            "position": 13,
                            "broker_bracket_required": True,
                        },
                    ],
                },
            },
        },
        manual_ack={
            "schema_version": 2,
            "kind": "eta_broker_bracket_manual_oco_ack_ledger",
            "acks": [
                {
                    "symbol": "MNQM6",
                    "venue": "ibkr",
                    "verified": True,
                    "operator": "edward",
                    "verified_at_utc": datetime.now(UTC).isoformat(),
                    "expires_at_utc": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            ],
        },
    )

    assert report["summary"] == "BLOCKED_UNBRACKETED_EXPOSURE"
    assert report["ready_for_prop_dry_run"] is False
    assert report["position_summary"]["missing_bracket_count"] == 1
    assert report["position_summary"]["manual_oco_verified_symbols"] == ["MNQM6"]
    assert report["position_summary"]["unprotected_symbols"] == ["MCLM6"]
    assert report["primary_unprotected_position"]["symbol"] == "MCLM6"


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
