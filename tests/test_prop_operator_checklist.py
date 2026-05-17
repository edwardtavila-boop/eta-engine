"""Tests for the prop operator checklist artifact."""

from __future__ import annotations

from pathlib import Path

import pytest

from eta_engine.scripts import prop_operator_checklist as checklist
from eta_engine.scripts import workspace_roots


def _blocked_gate_report() -> dict[str, object]:
    return {
        "kind": "eta_prop_live_readiness_gate",
        "summary": "BLOCKED",
        "primary_bot": "volume_profile_mnq",
        "scope_family": "futures_prop_ladder",
        "scope_mode": "controlled_prop_dry_run",
        "scope_note": (
            "This gate governs the futures prop-ladder controlled dry-run lane "
            "for volume_profile_mnq. Diamond or Wave-25 launch candidacy is "
            "tracked separately and can remain NO_GO independently."
        ),
        "parallel_launch_surface": "eta_engine.scripts.prop_launch_check",
        "parallel_launch_scope": "diamond_wave25_launch_readiness",
        "parallel_launch_note": (
            "Use eta_engine.scripts.prop_launch_check for Diamond and Wave-25 launch-candidate truth."
        ),
        "checks": [
            {
                "name": "prop_readiness",
                "status": "BLOCKED",
                "detail": "prop readiness is BLOCKED, not READY_FOR_DRY_RUN",
                "evidence": {
                    "prop_account": "blusky_50k",
                    "phase": "cutover",
                    "venue_policy": "tradovate_dormant",
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
                    "position_summary": {
                        "unprotected_symbols": ["MNQM6", "MCLM6", "NQM6"],
                    },
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
            "Record proof after confirming OCO.",
            "Keep volume_profile_mnq in paper_soak.",
            "Tradovate remains DORMANT.",
        ],
    }


def test_prop_operator_checklist_turns_gate_blockers_into_operator_steps() -> None:
    report = checklist.build_checklist_report(gate_report=_blocked_gate_report())

    assert report["summary"] == "BLOCKED"
    assert report["scope_family"] == "futures_prop_ladder"
    assert report["scope_mode"] == "controlled_prop_dry_run"
    assert report["scope_summary"] == "futures_prop_ladder/controlled_prop_dry_run for volume_profile_mnq"
    assert report["parallel_launch_surface"] == "eta_engine.scripts.prop_launch_check"
    assert report["parallel_launch_scope"] == "diamond_wave25_launch_readiness"
    assert report["parallel_launch_command"] == "python -m eta_engine.scripts.prop_launch_check --json"
    assert report["parallel_lane_hint"] == (
        "Separate lane: diamond_wave25_launch_readiness via eta_engine.scripts.prop_launch_check"
    )
    assert report["can_start_prop_dry_run"] is False
    assert report["blocking_step_count"] == 3
    assert [step["id"] for step in report["checklist"]] == [
        "verify_manual_oco_or_flatten",
        "hold_primary_paper_soak",
        "hold_tradovate_dormant",
    ]
    steps = {step["id"]: step for step in report["checklist"]}
    assert steps["hold_tradovate_dormant"]["command"] == "no-op: Tradovate stays dormant until explicit reactivation"
    assert steps["hold_tradovate_dormant"]["missing_secrets"] == [
        "BLUSKY_TRADOVATE_ACCOUNT_ID",
        "BLUSKY_TRADOVATE_APP_SECRET",
    ]
    assert steps["hold_tradovate_dormant"]["order_action"] is False
    assert steps["verify_manual_oco_or_flatten"]["command"] == (
        "python -m eta_engine.scripts.broker_bracket_audit "
        "--ack-manual-oco --symbol MNQM6 --venue ibkr --operator edward "
        "--expires-hours 24 --confirm"
    )
    assert steps["verify_manual_oco_or_flatten"]["unprotected_symbols"] == ["MNQM6", "MCLM6", "NQM6"]
    assert steps["verify_manual_oco_or_flatten"]["ack_manual_oco_commands"] == [
        (
            "python -m eta_engine.scripts.broker_bracket_audit "
            "--ack-manual-oco --symbol MNQM6 --venue ibkr --operator edward "
            "--expires-hours 24 --confirm"
        ),
        (
            "python -m eta_engine.scripts.broker_bracket_audit "
            "--ack-manual-oco --symbol MCLM6 --venue ibkr --operator edward "
            "--expires-hours 24 --confirm"
        ),
        (
            "python -m eta_engine.scripts.broker_bracket_audit "
            "--ack-manual-oco --symbol NQM6 --venue ibkr --operator edward "
            "--expires-hours 24 --confirm"
        ),
    ]
    assert steps["verify_manual_oco_or_flatten"]["order_action"] is False
    assert steps["verify_manual_oco_or_flatten"]["alternative_order_action"] is True
    assert steps["hold_primary_paper_soak"]["bot_id"] == "volume_profile_mnq"
    assert steps["hold_primary_paper_soak"]["launch_lane"] == "paper_soak"
    assert steps["hold_primary_paper_soak"]["scope_family"] == "futures_prop_ladder"
    assert steps["hold_primary_paper_soak"]["scope_mode"] == "controlled_prop_dry_run"
    assert "Diamond or Wave-25 launch candidacy" in steps["hold_primary_paper_soak"]["scope_note"]
    assert steps["hold_primary_paper_soak"]["parallel_launch_surface"] == "eta_engine.scripts.prop_launch_check"
    assert steps["hold_primary_paper_soak"]["parallel_launch_scope"] == "diamond_wave25_launch_readiness"
    assert steps["hold_primary_paper_soak"]["parallel_launch_note"] == (
        "Use eta_engine.scripts.prop_launch_check for Diamond and Wave-25 launch-candidate truth."
    )
    assert steps["hold_primary_paper_soak"]["parallel_launch_command"] == (
        "python -m eta_engine.scripts.prop_launch_check --json"
    )
    assert steps["hold_primary_paper_soak"]["promotion_audit_command"] == (
        "python -m eta_engine.scripts.prop_strategy_promotion_audit --json"
    )


def test_prop_operator_checklist_ready_has_no_blocking_steps() -> None:
    gate_report = {
        "kind": "eta_prop_live_readiness_gate",
        "summary": "READY_FOR_CONTROLLED_PROP_DRY_RUN",
        "primary_bot": "volume_profile_mnq",
        "scope_family": "futures_prop_ladder",
        "scope_mode": "controlled_prop_dry_run",
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
    assert report["parallel_launch_command"] == "python -m eta_engine.scripts.prop_launch_check --json"
    assert report["checklist"] == []


def test_cli_rejects_output_path_outside_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_workspace = tmp_path / "workspace"
    outside_workspace = tmp_path / "outside" / "prop_operator_checklist_latest.json"
    fake_workspace.mkdir()
    monkeypatch.setattr(workspace_roots, "WORKSPACE_ROOT", fake_workspace)
    monkeypatch.setattr(
        checklist,
        "_current_gate_report",
        lambda: (_ for _ in ()).throw(AssertionError("gate report should not load")),
    )

    with pytest.raises(SystemExit) as exc:
        checklist.main(["--out", str(outside_workspace)])

    assert exc.value.code == 2
