"""Tests for read-only broker bracket/OCO coverage audit."""

from __future__ import annotations

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
