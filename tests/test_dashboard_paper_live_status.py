from __future__ import annotations

from eta_engine.deploy.scripts.dashboard_paper_live_status import (
    reconcile_paper_live_transition_launch_block,
    resolve_paper_live_card,
    resolve_paper_live_effective_state,
)


def test_resolve_paper_live_effective_state_surfaces_bracket_audit_detail() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=True,
        bracket_audit_detail="held by Bracket Audit: Flatten unprotected exposure",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
    )

    assert resolved["effective_status"] == "held_by_bracket_audit"
    assert resolved["effective_detail"] == "held by Bracket Audit: Flatten unprotected exposure"


def test_resolve_paper_live_effective_state_promotes_ready_advisory_to_shadow_paper() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=True,
        daily_loss_shadow_detail="Shadow paper remains live until reset",
        daily_loss_hold_detail="hold detail",
    )

    assert resolved["effective_status"] == "shadow_paper_active"
    assert resolved["effective_detail"] == "Shadow paper remains live until reset"


def test_resolve_paper_live_effective_state_keeps_daily_loss_hold_over_shadow_runtime() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=True,
        daily_loss_advisory_active=False,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="Global daily loss stop active",
        shadow_runtime_active=True,
        shadow_runtime_detail="live shadow paper lane active on 2 attached bot(s)",
    )

    assert resolved["effective_status"] == "held_by_daily_loss_stop"
    assert resolved["effective_detail"] == "Global daily loss stop active"


def test_resolve_paper_live_effective_state_uses_shadow_attached_count_when_safe() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="blocked",
        effective_detail="still warming up",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
        shadow_paper_attached_count=2,
    )

    assert resolved["effective_status"] == "shadow_paper_active"
    assert resolved["effective_detail"] == "live shadow paper lane active on 2 attached bot(s)"


def test_resolve_paper_live_effective_state_stale_receipt_wins_last() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="fresh",
        operator_queue_launch_blocked_count=0,
        operator_queue_blocked_detail="",
        stale_receipt=True,
        stale_detail="receipt is stale",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=True,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
        shadow_runtime_active=True,
        shadow_runtime_detail="runtime shadow detail",
    )

    assert resolved["effective_status"] == "stale_receipt"
    assert resolved["effective_detail"] == "receipt is stale"


def test_resolve_paper_live_effective_state_surfaces_operator_queue_launch_blocker() -> None:
    resolved = resolve_paper_live_effective_state(
        raw_status="ready_to_launch_paper_live",
        effective_detail="",
        operator_queue_launch_blocked_count=1,
        operator_queue_blocked_detail="Seed IBC credentials and recover TWS API 4002.",
        stale_receipt=False,
        stale_detail="",
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        daily_loss_shadow_detail="shadow detail",
        daily_loss_hold_detail="hold detail",
    )

    assert resolved["effective_status"] == "blocked_by_operator_queue"
    assert resolved["effective_detail"] == "Seed IBC credentials and recover TWS API 4002."


def test_resolve_paper_live_card_marks_ready_runtime_green() -> None:
    resolved = resolve_paper_live_card(
        effective_status="ready_to_launch_paper_live",
        stale_receipt=False,
        stale_detail="",
        non_authoritative_gateway_host=False,
        launch_blocked_count=0,
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        critical_ready=True,
    )

    assert resolved["status"] == "GREEN"
    assert resolved["detail"] == "ready_to_launch_paper_live"


def test_resolve_paper_live_card_prefers_stale_detail() -> None:
    resolved = resolve_paper_live_card(
        effective_status="stale_receipt",
        stale_receipt=True,
        stale_detail="receipt is stale",
        non_authoritative_gateway_host=False,
        launch_blocked_count=0,
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        critical_ready=True,
    )

    assert resolved["status"] == "YELLOW"
    assert resolved["detail"] == "receipt is stale"


def test_resolve_paper_live_card_surfaces_non_authoritative_block_detail() -> None:
    resolved = resolve_paper_live_card(
        effective_status="blocked",
        stale_receipt=False,
        stale_detail="",
        non_authoritative_gateway_host=True,
        launch_blocked_count=1,
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        critical_ready=False,
        blocked_detail="On the VPS only: apply gateway authority.",
    )

    assert resolved["status"] == "YELLOW"
    assert resolved["detail"] == "On the VPS only: apply gateway authority."


def test_resolve_paper_live_card_marks_shadow_runtime_yellow() -> None:
    resolved = resolve_paper_live_card(
        effective_status="shadow_paper_active",
        stale_receipt=False,
        stale_detail="",
        non_authoritative_gateway_host=False,
        launch_blocked_count=1,
        held_by_bracket_audit=False,
        held_by_daily_loss_stop=False,
        daily_loss_advisory_active=False,
        critical_ready=False,
        shadow_runtime_active=True,
    )

    assert resolved["status"] == "YELLOW"
    assert resolved["detail"] == "shadow_paper_active"


def test_reconcile_paper_live_transition_launch_block_prefers_fresh_queue_when_cache_stale() -> None:
    resolved = reconcile_paper_live_transition_launch_block(
        paper_live_transition={
            "cache_stale": True,
            "operator_queue_launch_blocked_count": 1,
            "operator_queue_first_launch_blocker_op_id": "OP-18",
            "operator_queue_first_launch_next_action": "python -m stale_probe",
        },
        operator_queue={
            "cache_stale": False,
            "launch_blocked_count": 1,
        },
        first_launch_blocker={
            "op_id": "OP-19",
            "detail": "Seed IBC credentials and recover TWS API 4002.",
        },
    )

    assert resolved["launch_blocked_count"] == 1
    assert resolved["first_launch_blocker_op_id"] == "OP-19"
    assert resolved["first_launch_next_action"] == "Seed IBC credentials and recover TWS API 4002."


def test_reconcile_paper_live_transition_launch_block_fills_missing_action_from_fresh_queue() -> None:
    resolved = reconcile_paper_live_transition_launch_block(
        paper_live_transition={
            "operator_queue_launch_blocked_count": 1,
            "operator_queue_first_launch_blocker_op_id": "",
            "operator_queue_first_launch_next_action": "",
        },
        operator_queue={
            "cache_stale": False,
            "launch_blocked_count": 1,
        },
        first_launch_blocker={
            "op_id": "OP-20",
            "next_actions": [
                "Do not unlock new entries until broker/supervisor positions reconcile.",
            ],
            "detail": "3 broker/supervisor mismatch(es).",
        },
    )

    assert resolved["launch_blocked_count"] == 1
    assert resolved["first_launch_blocker_op_id"] == "OP-20"
    assert (
        resolved["first_launch_next_action"]
        == "Do not unlock new entries until broker/supervisor positions reconcile."
    )
