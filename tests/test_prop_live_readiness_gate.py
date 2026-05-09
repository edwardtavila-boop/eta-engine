"""Tests for the consolidated prop-live go/no-go gate."""

from __future__ import annotations

from eta_engine.scripts import prop_live_readiness_gate as gate


def _ready_payloads() -> dict[str, object]:
    return {
        "ladder": {
            "summary": {
                "primary_bot": "volume_profile_mnq",
                "automation_mode": "PRIMARY_READY_FOR_CONTROLLED_PROP_DRY_RUN",
                "live_routing_allowed_count": 1,
            },
            "candidates": [
                {
                    "bot_id": "volume_profile_mnq",
                    "role": "primary",
                    "live_routing_allowed": True,
                    "blockers": [],
                },
            ],
        },
        "prop": {"summary": "READY_FOR_DRY_RUN"},
        "master": {
            "systems": {
                "ibkr": {"status": "GREEN"},
                "broker": {"status": "GREEN", "active_blocker_count": 0},
                "paper_live": {"status": "GREEN"},
            },
        },
        "fleet": {
            "broker_router": {
                "active_blocker_count": 0,
                "failed_count": 0,
                "quarantine_count": 0,
                "result_status_counts": {"REJECTED": 0},
            },
            "summary": {
                "broker_open_position_count": 0,
                "broker_bracket_count": 0,
                "supervisor_local_position_count": 0,
            },
            "bots": [
                {
                    "id": "volume_profile_mnq",
                    "can_live_trade": True,
                    "broker_bracket": True,
                    "open_positions": 0,
                },
            ],
        },
        "ledger": {"closed_trade_count": 25, "schema_version": 1},
    }


def test_prop_live_gate_ready_when_every_surface_is_green() -> None:
    payloads = _ready_payloads()

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "READY_FOR_CONTROLLED_PROP_DRY_RUN"
    assert gate.exit_code(report) == 0
    assert all(check["status"] == "PASS" for check in report["checks"])


def test_prop_live_gate_blocks_dirty_router_and_missing_ledger() -> None:
    payloads = _ready_payloads()
    payloads["fleet"]["broker_router"]["active_blocker_count"] = 1
    payloads["fleet"]["broker_router"]["failed_count"] = 37
    payloads["fleet"]["broker_router"]["quarantine_count"] = 15
    payloads["ledger"] = {}

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "BLOCKED"
    assert gate.exit_code(report) == 1
    assert any(check["name"] == "router_cleanliness" and check["status"] == "BLOCKED" for check in report["checks"])
    assert any(check["name"] == "closed_trade_ledger" and check["status"] == "BLOCKED" for check in report["checks"])


def test_prop_live_gate_allows_historical_router_residue_when_active_clean() -> None:
    payloads = _ready_payloads()
    payloads["fleet"]["broker_router"]["failed_count"] = 37
    payloads["fleet"]["broker_router"]["quarantine_count"] = 15
    payloads["fleet"]["broker_router"]["result_status_counts"]["REJECTED"] = 8

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "READY_FOR_CONTROLLED_PROP_DRY_RUN"
    assert any(check["name"] == "router_cleanliness" and check["status"] == "PASS" for check in report["checks"])


def test_prop_live_gate_blocks_runner_or_unbracketed_live_path() -> None:
    payloads = _ready_payloads()
    payloads["ladder"]["summary"]["live_routing_allowed_count"] = 0
    payloads["ladder"]["candidates"][0]["live_routing_allowed"] = False
    payloads["fleet"]["summary"]["supervisor_local_position_count"] = 1
    payloads["fleet"]["summary"]["broker_bracket_count"] = 0
    payloads["fleet"]["bots"][0]["can_live_trade"] = False

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "BLOCKED"
    assert any(check["name"] == "primary_ladder" and check["status"] == "BLOCKED" for check in report["checks"])
    assert any(check["name"] == "broker_native_brackets" and check["status"] == "BLOCKED" for check in report["checks"])
    assert any(check["name"] == "live_bot_gate" and check["status"] == "BLOCKED" for check in report["checks"])
