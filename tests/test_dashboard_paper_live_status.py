from __future__ import annotations

from eta_engine.deploy.scripts.dashboard_paper_live_status import resolve_paper_live_effective_state


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
