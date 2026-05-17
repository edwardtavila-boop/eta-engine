"""Tests for the consolidated prop-live go/no-go gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from eta_engine.scripts import prop_live_readiness_gate as gate
from eta_engine.scripts import workspace_roots


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
    assert report["scope_family"] == "futures_prop_ladder"
    assert report["scope_mode"] == "controlled_prop_dry_run"
    assert report["scope_primary_bot"] == "volume_profile_mnq"
    assert report["parallel_launch_surface"] == "eta_engine.scripts.prop_launch_check"
    assert report["parallel_launch_scope"] == "diamond_wave25_launch_readiness"
    assert "Diamond or Wave-25 launch candidacy" in report["scope_note"]
    assert gate.exit_code(report) == 0
    assert all(check["status"] == "PASS" for check in report["checks"])


def test_build_current_broker_bracket_audit_enables_live_stale_order_validation(monkeypatch) -> None:
    from eta_engine.scripts import broker_bracket_audit

    calls: dict[str, object] = {}

    monkeypatch.setattr(broker_bracket_audit, "load_manual_oco_ack", lambda: {"entries": []})

    def _fake_build_bracket_audit(*, fleet, manual_ack, validate_live_stale_orders=False):
        calls["fleet"] = fleet
        calls["manual_ack"] = manual_ack
        calls["validate_live_stale_orders"] = validate_live_stale_orders
        return {"summary": "READY_NO_OPEN_EXPOSURE"}

    monkeypatch.setattr(broker_bracket_audit, "build_bracket_audit", _fake_build_bracket_audit)

    report = gate._build_current_broker_bracket_audit({"summary": {"broker_open_position_count": 0}})

    assert report["summary"] == "READY_NO_OPEN_EXPOSURE"
    assert calls["validate_live_stale_orders"] is True


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


def test_prop_live_gate_accepts_shadow_paper_active_when_critical_ready() -> None:
    payloads = _ready_payloads()
    payloads["master"]["systems"]["paper_live"] = {
        "status": "YELLOW",
        "critical_ready": True,
        "effective_status": "shadow_paper_active",
        "effective_detail": "live shadow paper lane active on 9 attached bot(s)",
        "held_by_bracket_audit": False,
        "held_by_daily_loss_stop": False,
    }

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "READY_FOR_CONTROLLED_PROP_DRY_RUN"
    surface_check = next(check for check in report["checks"] if check["name"] == "broker_surfaces")
    assert surface_check["status"] == "PASS"
    assert "shadow paper lane is active" in surface_check["detail"]


def test_prop_live_gate_does_not_double_count_stale_flat_order_bracket_hold() -> None:
    payloads = _ready_payloads()
    payloads["master"]["systems"]["broker"] = {
        "status": "YELLOW",
        "raw_status": "ok",
        "target_exit_status": "watching",
        "active_blocker_count": 0,
    }
    payloads["master"]["systems"]["paper_live"] = {
        "status": "YELLOW",
        "critical_ready": True,
        "held_by_bracket_audit": True,
        "held_by_daily_loss_stop": False,
        "effective_status": "shadow_paper_active",
        "effective_detail": "live shadow paper lane active on 9 attached bot(s)",
    }
    payloads["fleet"]["summary"]["broker_open_position_count"] = 1
    payloads["fleet"]["summary"]["broker_bracket_count"] = 1
    payloads["broker_bracket_audit"] = {
        "summary": "BLOCKED_STALE_FLAT_OPEN_ORDERS",
        "ready_for_prop_dry_run": False,
        "next_action": "Cancel stale active broker order(s) for NQM6",
        "position_summary": {
            "broker_open_position_count": 1,
            "broker_bracket_count": 1,
            "missing_bracket_count": 0,
            "stale_flat_open_order_count": 2,
        },
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


def test_prop_live_gate_ignores_dormant_tradovate_only_secret_gap() -> None:
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

    report = gate.build_gate_report(**payloads)

    assert report["summary"] == "READY_FOR_CONTROLLED_PROP_DRY_RUN"
    prop_check = next(check for check in report["checks"] if check["name"] == "prop_readiness")
    assert prop_check["status"] == "PASS"
    assert prop_check["evidence"]["venue_policy"] == "tradovate_dormant"


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
    assert "setup_tradovate_secrets --prop-account blusky_50k" not in actions
    assert "Tradovate remains DORMANT" not in actions
    assert prop_check["evidence"]["venue_policy"] == "tradovate_dormant"
    assert prop_check["status"] == "PASS"
    assert prop_check["evidence"]["missing_secrets"] == [
        "BLUSKY_TRADOVATE_ACCOUNT_ID",
        "BLUSKY_TRADOVATE_APP_SECRET",
    ]
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


def test_load_gate_inputs_uses_broker_bracket_audit_fleet_loader(monkeypatch) -> None:
    from eta_engine.scripts import broker_bracket_audit

    calls: dict[str, object] = {}

    monkeypatch.setattr(gate, "_build_current_prop", lambda _prop_account: {})
    monkeypatch.setattr(gate, "_build_current_ladder", lambda _prop: {})
    monkeypatch.setattr(gate, "_build_current_ledger", lambda: {})
    monkeypatch.setattr(gate, "_build_current_broker_bracket_audit", lambda fleet: {"fleet": fleet})
    monkeypatch.setattr(gate, "_fetch_json", lambda _url, timeout_s=10.0, attempts=2: {"status": "ok"})

    def _fake_load_fleet_payload(url: str) -> dict[str, object]:
        calls["fleet_url"] = url
        return {"summary": {"broker_open_position_count": 3}}

    monkeypatch.setattr(broker_bracket_audit, "load_fleet_payload", _fake_load_fleet_payload)

    inputs = gate.load_gate_inputs(fleet_url="https://ops.example.invalid/api/bot-fleet")

    assert calls["fleet_url"] == "https://ops.example.invalid/api/bot-fleet"
    assert inputs["fleet"]["summary"]["broker_open_position_count"] == 3
    assert inputs["broker_bracket_audit"]["fleet"]["summary"]["broker_open_position_count"] == 3


def test_load_gate_inputs_uses_local_dashboard_fleet_fallback_when_http_truth_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_build_current_prop", lambda _prop_account: {})
    monkeypatch.setattr(gate, "_build_current_ladder", lambda _prop: {})
    monkeypatch.setattr(gate, "_build_current_ledger", lambda: {})
    monkeypatch.setattr(gate, "_fetch_json", lambda _url, timeout_s=10.0, attempts=2: {"status": "ok"})
    monkeypatch.setattr(gate, "_load_fleet_payload", lambda _url: {})
    monkeypatch.setattr(
        gate,
        "_load_local_dashboard_fleet_payload",
        lambda: {"summary": {"broker_open_position_count": 1}, "bots": [{"id": "volume_profile_nq"}]},
    )
    monkeypatch.setattr(
        gate,
        "_build_current_broker_bracket_audit",
        lambda fleet: (
            {"summary": "BLOCKED_FLEET_TRUTH_UNAVAILABLE", "fleet": fleet}
            if not fleet
            else {"summary": "READY_NO_OPEN_EXPOSURE", "fleet": fleet}
        ),
    )

    inputs = gate.load_gate_inputs(fleet_url="https://ops.example.invalid/api/bot-fleet")

    assert inputs["fleet"]["summary"]["broker_open_position_count"] == 1
    assert inputs["broker_bracket_audit"]["summary"] == "READY_NO_OPEN_EXPOSURE"
    assert inputs["broker_bracket_audit"]["fleet"]["summary"]["broker_open_position_count"] == 1


def test_load_gate_inputs_uses_local_master_and_live_readiness_fallbacks_when_public_fetch_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gate, "_build_current_prop", lambda _prop_account: {})
    monkeypatch.setattr(gate, "_build_current_ladder", lambda _prop: {})
    monkeypatch.setattr(gate, "_build_current_ledger", lambda: {})
    monkeypatch.setattr(gate, "_load_fleet_payload", lambda _url: {"summary": {"broker_open_position_count": 0}})
    monkeypatch.setattr(gate, "_build_current_broker_bracket_audit", lambda fleet: {"summary": "READY", "fleet": fleet})
    monkeypatch.setattr(gate, "_fetch_json", lambda _url, timeout_s=10.0, attempts=2: {})
    monkeypatch.setattr(
        gate,
        "_load_local_master_payload",
        lambda: {
            "systems": {
                "ibkr": {"status": "GREEN"},
                "broker": {"status": "GREEN"},
                "paper_live": {"status": "GREEN"},
            },
        },
    )
    monkeypatch.setattr(
        gate,
        "_load_local_live_readiness_payload",
        lambda: {"source": "bot_strategy_readiness", "found": True, "bot_id": "volume_profile_mnq"},
    )

    inputs = gate.load_gate_inputs()

    assert inputs["master"]["systems"]["ibkr"]["status"] == "GREEN"
    assert inputs["live_readiness"]["source"] == "bot_strategy_readiness"
    assert inputs["live_readiness"]["found"] is True


def test_cli_rejects_output_path_outside_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "prop_live_readiness_latest.json"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        gate,
        "load_gate_inputs",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("gate inputs should not load")),
    )

    with pytest.raises(SystemExit) as exc:
        gate.main(["--out", str(outside_workspace)])

    assert exc.value.code == 2
