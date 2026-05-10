"""Tests for the prop operator checklist artifact."""

from __future__ import annotations

from eta_engine.scripts import prop_operator_checklist as checklist


def _blocked_gate_report() -> dict[str, object]:
    return {
        "kind": "eta_prop_live_readiness_gate",
        "summary": "BLOCKED",
        "primary_bot": "volume_profile_mnq",
        "checks": [
            {
                "name": "prop_readiness",
                "status": "BLOCKED",
                "detail": "prop readiness is BLOCKED, not READY_FOR_DRY_RUN",
                "evidence": {
                    "prop_account": "blusky_50k",
                    "phase": "cutover",
                    "missing_secrets": [
                        "BLUSKY_TRADOVATE_ACCOUNT_ID",
                        "BLUSKY_TRADOVATE_APP_SECRET",
                    ],
                },
            },
            {
                "name": "broker_native_brackets",
                "status": "BLOCKED",
                "detail": "MNQM6 missing broker-native OCO",
                "evidence": {
                    "primary_unprotected_position": {
                        "symbol": "MNQM6",
                        "venue": "ibkr",
                        "sec_type": "FUT",
                    },
                },
            },
            {
                "name": "live_bot_gate",
                "status": "BLOCKED",
                "detail": "volume_profile_mnq is visible but still not marked can_live_trade",
                "evidence": {"launch_lane": "paper_soak", "bot_status": "running"},
            },
        ],
        "next_actions": [
            "Seed Tradovate API secrets after funding/API unlock.",
            "Keep volume_profile_mnq in paper_soak.",
            "Record proof after confirming OCO.",
        ],
    }


def test_prop_operator_checklist_turns_gate_blockers_into_operator_steps() -> None:
    report = checklist.build_checklist_report(gate_report=_blocked_gate_report())

    assert report["summary"] == "BLOCKED"
    assert report["can_start_prop_dry_run"] is False
    assert report["blocking_step_count"] == 3
    steps = {step["id"]: step for step in report["checklist"]}
    assert steps["seed_tradovate_api_secrets"]["command"] == (
        "python -m eta_engine.scripts.setup_tradovate_secrets --prop-account blusky_50k"
    )
    assert steps["seed_tradovate_api_secrets"]["missing_secrets"] == [
        "BLUSKY_TRADOVATE_ACCOUNT_ID",
        "BLUSKY_TRADOVATE_APP_SECRET",
    ]
    assert steps["verify_manual_oco_or_flatten"]["command"] == (
        "python -m eta_engine.scripts.broker_bracket_audit "
        "--ack-manual-oco --symbol MNQM6 --venue ibkr --operator edward "
        "--expires-hours 24 --confirm"
    )
    assert steps["verify_manual_oco_or_flatten"]["order_action"] is False
    assert steps["verify_manual_oco_or_flatten"]["alternative_order_action"] is True
    assert steps["hold_primary_paper_soak"]["bot_id"] == "volume_profile_mnq"
    assert steps["hold_primary_paper_soak"]["launch_lane"] == "paper_soak"
    assert steps["hold_primary_paper_soak"]["promotion_audit_command"] == (
        "python -m eta_engine.scripts.prop_strategy_promotion_audit --json"
    )


def test_prop_operator_checklist_ready_has_no_blocking_steps() -> None:
    gate_report = {
        "kind": "eta_prop_live_readiness_gate",
        "summary": "READY_FOR_CONTROLLED_PROP_DRY_RUN",
        "primary_bot": "volume_profile_mnq",
        "checks": [
            {"name": "prop_readiness", "status": "PASS", "detail": "ready"},
            {"name": "broker_native_brackets", "status": "PASS", "detail": "ready"},
            {"name": "live_bot_gate", "status": "PASS", "detail": "ready"},
        ],
        "next_actions": ["Run the controlled no-live-money dry run."],
    }

    report = checklist.build_checklist_report(gate_report=gate_report)

    assert report["summary"] == "READY_FOR_CONTROLLED_PROP_DRY_RUN"
    assert report["can_start_prop_dry_run"] is True
    assert report["blocking_step_count"] == 0
    assert report["checklist"] == []
