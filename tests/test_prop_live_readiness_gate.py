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


def test_prop_live_gate_accepts_primary_bot_id_alias() -> None:
    payloads = _ready_payloads()
    payloads["fleet"]["bots"][0].pop("id")
    payloads["fleet"]["bots"][0]["bot_id"] = "volume_profile_mnq"

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "READY_FOR_CONTROLLED_PROP_DRY_RUN"
    live_check = next(check for check in report["checks"] if check["name"] == "live_bot_gate")
    assert live_check["status"] == "PASS"


def test_prop_live_gate_reports_live_readiness_deactivation_drift() -> None:
    payloads = _ready_payloads()
    payloads["fleet"]["bots"] = [
        {
            "id": "volume_profile_nq",
            "can_live_trade": False,
            "launch_lane": "paper_soak",
            "status": "readiness_only",
        },
    ]
    payloads["live_readiness"] = {
        "found": True,
        "bot_id": "volume_profile_mnq",
        "readiness_next_action": "No action: bot is explicitly deactivated.",
        "row": {
            "bot_id": "volume_profile_mnq",
            "active": False,
            "launch_lane": "deactivated",
            "data_status": "deactivated",
            "promotion_status": "deactivated",
            "deactivation_source": "kaizen_sidecar",
            "deactivation_reason": "DECAY; DEAD",
        },
    }

    report = gate.build_gate_report(**payloads)
    live_check = next(check for check in report["checks"] if check["name"] == "live_bot_gate")
    actions = "\n".join(report["next_actions"])

    assert report["summary"] == "BLOCKED"
    assert live_check["status"] == "BLOCKED"
    assert "deactivated on the live readiness surface" in live_check["detail"]
    assert live_check["evidence"]["live_readiness_active"] is False
    assert live_check["evidence"]["live_readiness_launch_lane"] == "deactivated"
    assert live_check["evidence"]["live_readiness_has_deactivation_provenance"] is True
    assert live_check["evidence"]["live_readiness_deactivation_source"] == "kaizen_sidecar"
    assert live_check["evidence"]["visible_related_bots"] == ["volume_profile_nq"]
    assert "intentionally retired by Kaizen" in actions
    assert "do not reconcile it back to paper-soak" in actions
    assert "runner-up/Kaizen ELITE review" in actions
    assert "via kaizen_sidecar" in actions
    assert "No action: bot is explicitly deactivated." in actions


def test_prop_live_gate_flags_stale_live_readiness_schema() -> None:
    payloads = _ready_payloads()
    payloads["fleet"]["bots"] = [{"id": "volume_profile_nq", "can_live_trade": False}]
    payloads["live_readiness"] = {
        "found": True,
        "bot_id": "volume_profile_mnq",
        "readiness_next_action": "No action: bot is explicitly deactivated.",
        "row": {
            "bot_id": "volume_profile_mnq",
            "active": False,
            "launch_lane": "deactivated",
            "data_status": "deactivated",
            "promotion_status": "deactivated",
        },
    }

    report = gate.build_gate_report(**payloads)
    live_check = next(check for check in report["checks"] if check["name"] == "live_bot_gate")
    actions = "\n".join(report["next_actions"])

    assert live_check["evidence"]["live_readiness_has_deactivation_provenance"] is False
    assert "Refresh the VPS eta_engine code" in actions
    assert "registry_extras versus kaizen_sidecar" in actions
    assert "Live readiness action: No action" not in actions


def test_prop_live_gate_accepts_manual_oco_verified_bracket_audit() -> None:
    payloads = _ready_payloads()
    payloads["fleet"]["summary"]["broker_open_position_count"] = 1
    payloads["fleet"]["summary"]["broker_bracket_count"] = 0
    payloads["fleet"]["summary"]["supervisor_local_position_count"] = 1
    payloads["broker_bracket_audit"] = {
        "summary": "READY_OPEN_EXPOSURE_MANUAL_OCO_VERIFIED",
        "ready_for_prop_dry_run": True,
        "position_summary": {
            "broker_open_position_count": 1,
            "broker_bracket_count": 0,
            "missing_bracket_count": 0,
            "manual_oco_verified_count": 1,
            "manual_oco_verified_symbols": ["MNQM6"],
        },
    }

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "READY_FOR_CONTROLLED_PROP_DRY_RUN"
    bracket_check = next(check for check in report["checks"] if check["name"] == "broker_native_brackets")
    assert bracket_check["status"] == "PASS"
    assert "manual OCO verification" in bracket_check["detail"]


def test_prop_live_gate_does_not_double_count_bracket_hold_as_broker_surface_failure() -> None:
    payloads = _ready_payloads()
    payloads["master"]["systems"]["broker"] = {
        "status": "YELLOW",
        "raw_status": "ok",
        "target_exit_status": "missing_brackets",
        "active_blocker_count": 0,
    }
    payloads["master"]["systems"]["paper_live"] = {
        "status": "YELLOW",
        "critical_ready": True,
        "held_by_bracket_audit": True,
        "effective_status": "held_by_bracket_audit",
    }
    payloads["fleet"]["summary"]["broker_open_position_count"] = 1
    payloads["fleet"]["summary"]["broker_bracket_count"] = 0
    payloads["fleet"]["summary"]["supervisor_local_position_count"] = 1
    payloads["broker_bracket_audit"] = {
        "summary": "BLOCKED_UNBRACKETED_EXPOSURE",
        "ready_for_prop_dry_run": False,
        "next_action": "MNQM6 missing broker-native OCO",
    }

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "BLOCKED"
    surface_check = next(check for check in report["checks"] if check["name"] == "broker_surfaces")
    bracket_check = next(check for check in report["checks"] if check["name"] == "broker_native_brackets")
    assert surface_check["status"] == "PASS"
    assert "held by bracket audit" in surface_check["detail"]
    assert bracket_check["status"] == "BLOCKED"


def test_prop_live_gate_blocks_missing_fleet_truth_in_bracket_audit() -> None:
    payloads = _ready_payloads()
    payloads["broker_bracket_audit"] = {
        "summary": "BLOCKED_FLEET_TRUTH_UNAVAILABLE",
        "ready_for_prop_dry_run": False,
        "next_action": "Bot-fleet position truth is unavailable",
        "position_summary": {},
    }

    report = gate.build_gate_report(**payloads)
    actions = "\n".join(report["next_actions"])

    assert report["summary"] == "BLOCKED"
    bracket_check = next(check for check in report["checks"] if check["name"] == "broker_native_brackets")
    assert bracket_check["status"] == "BLOCKED"
    assert bracket_check["evidence"]["audit_summary"] == "BLOCKED_FLEET_TRUTH_UNAVAILABLE"
    assert "/api/bot-fleet" in actions
    assert "do not infer flat exposure" in actions


def test_prop_live_gate_next_actions_respect_dormant_tradovate_policy() -> None:
    payloads = _ready_payloads()
    payloads["prop"] = {
        "summary": "BLOCKED",
        "phase": "cutover",
        "prop_account": "blusky_50k",
        "secret_presence": {
            "missing": [
                "BLUSKY_TRADOVATE_ACCOUNT_ID",
                "BLUSKY_TRADOVATE_APP_SECRET",
            ],
        },
    }
    payloads["ladder"]["summary"]["live_routing_allowed_count"] = 0
    payloads["ladder"]["candidates"][0]["live_routing_allowed"] = False
    payloads["ladder"]["candidates"][0]["launch_lane"] = "paper_soak"
    payloads["ladder"]["candidates"][0]["blockers"] = ["bot row is not can_live_trade"]
    payloads["fleet"]["bots"][0]["can_live_trade"] = False
    payloads["fleet"]["bots"][0]["launch_lane"] = "paper_soak"
    payloads["broker_bracket_audit"] = {
        "summary": "BLOCKED_UNBRACKETED_EXPOSURE",
        "ready_for_prop_dry_run": False,
        "next_action": "MNQM6 missing broker-native OCO",
        "position_summary": {
            "unprotected_symbols": ["MNQM6", "MCLM6", "NQM6"],
        },
        "primary_unprotected_position": {
            "symbol": "MNQM6",
            "venue": "ibkr",
            "sec_type": "FUT",
        },
    }

    report = gate.build_gate_report(**payloads)
    actions_list = report["next_actions"]
    actions = "\n".join(actions_list)
    prop_check = next(check for check in report["checks"] if check["name"] == "prop_readiness")

    assert actions_list[0].startswith("After visually confirming broker-native TP/SL OCO")
    assert actions_list[-1].startswith("Tradovate remains DORMANT")
    assert "setup_tradovate_secrets --prop-account blusky_50k" not in actions
    assert "Tradovate remains DORMANT" in actions
    assert "explicit code/docs reactivation" in actions
    assert "BLUSKY_TRADOVATE_ACCOUNT_ID" in actions
    assert "BLUSKY_TRADOVATE_APP_SECRET" in actions
    assert prop_check["evidence"]["venue_policy"] == "tradovate_dormant"
    assert "--ack-manual-oco --symbol MNQM6 --venue ibkr" in actions
    assert "--ack-manual-oco --symbol MCLM6 --venue ibkr" in actions
    assert "--ack-manual-oco --symbol NQM6 --venue ibkr" in actions
    assert "paper_soak" in actions


def test_prop_live_gate_fetch_json_retries_transient_live_failure(monkeypatch) -> None:
    calls = {"count": 0}

    class _Response:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        @staticmethod
        def read() -> bytes:
            return b'{"truth_status":"live"}'

    def _flaky_urlopen(_request, *, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise gate.urllib.error.URLError("temporary timeout")
        return _Response()

    monkeypatch.setattr(gate.urllib.request, "urlopen", _flaky_urlopen)

    assert gate._fetch_json("https://ops.example.invalid/api/bot-fleet") == {"truth_status": "live"}  # noqa: SLF001
    assert calls["count"] == 2
