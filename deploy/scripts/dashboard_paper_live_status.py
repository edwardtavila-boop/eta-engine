"""Shared paper-live status helpers for the ETA dashboard."""

from __future__ import annotations

from typing import Any

_READY_PAPER_LIVE_STATUSES = frozenset(
    {
        "ready",
        "ready_to_launch_paper_live",
        "green",
    },
)


def resolve_paper_live_effective_state(
    *,
    raw_status: str,
    effective_detail: str,
    operator_queue_launch_blocked_count: int = 0,
    operator_queue_blocked_detail: str = "",
    stale_receipt: bool,
    stale_detail: str,
    held_by_bracket_audit: bool,
    bracket_audit_detail: str,
    held_by_daily_loss_stop: bool,
    daily_loss_advisory_active: bool,
    daily_loss_shadow_detail: str,
    daily_loss_hold_detail: str,
    shadow_runtime_active: bool = False,
    shadow_runtime_detail: str = "",
    shadow_paper_attached_count: int = 0,
) -> dict[str, Any]:
    """Resolve the operator-facing effective paper-live state.

    This keeps the dashboard's paper-live interpretation consistent across the
    master-status and bot-fleet summary surfaces.
    """

    effective_status = "held_by_bracket_audit" if held_by_bracket_audit else str(raw_status or "unknown")
    detail = str(effective_detail or "")

    if held_by_bracket_audit:
        detail = str(bracket_audit_detail or "held by Bracket Audit")

    if operator_queue_launch_blocked_count > 0 and effective_status in _READY_PAPER_LIVE_STATUSES:
        effective_status = "blocked_by_operator_queue"
        detail = str(operator_queue_blocked_detail or "Fresh operator queue has a launch blocker.")

    if daily_loss_advisory_active and effective_status in _READY_PAPER_LIVE_STATUSES:
        effective_status = "shadow_paper_active"
        detail = str(daily_loss_shadow_detail or detail)
    elif held_by_daily_loss_stop and effective_status in _READY_PAPER_LIVE_STATUSES:
        effective_status = "held_by_daily_loss_stop"
        detail = str(daily_loss_hold_detail or detail)

    if shadow_runtime_active and effective_status != "held_by_daily_loss_stop":
        effective_status = "shadow_paper_active"
        detail = str(shadow_runtime_detail or detail)
    elif shadow_paper_attached_count > 0 and effective_status not in {
        "held_by_bracket_audit",
        "held_by_daily_loss_stop",
    }:
        effective_status = "shadow_paper_active"
        detail = f"live shadow paper lane active on {shadow_paper_attached_count} attached bot(s)"

    if stale_receipt:
        effective_status = "stale_receipt"
        detail = str(stale_detail or detail)

    return {
        "effective_status": effective_status,
        "effective_detail": detail,
    }


def resolve_paper_live_card(
    *,
    effective_status: str,
    stale_receipt: bool,
    stale_detail: str,
    non_authoritative_gateway_host: bool,
    launch_blocked_count: int,
    held_by_bracket_audit: bool,
    held_by_daily_loss_stop: bool,
    daily_loss_advisory_active: bool,
    critical_ready: bool,
    shadow_runtime_active: bool = False,
    blocked_detail: str = "",
) -> dict[str, str]:
    """Resolve the paper-live system-card presentation.

    This is the operator-facing `systems.paper_live` layer that sits on top of
    the effective paper-live state.
    """

    card_status = (
        "YELLOW"
        if stale_receipt
        else "YELLOW"
        if non_authoritative_gateway_host and effective_status == "blocked"
        else "RED"
        if launch_blocked_count
        else "YELLOW"
        if held_by_bracket_audit or held_by_daily_loss_stop or daily_loss_advisory_active
        else "GREEN"
        if critical_ready
        else "YELLOW"
    )
    if shadow_runtime_active:
        card_status = "YELLOW"

    card_detail = str(effective_status or "unknown")
    if stale_receipt and stale_detail:
        card_detail = str(stale_detail)
    elif non_authoritative_gateway_host and effective_status == "blocked":
        card_detail = str(blocked_detail or card_detail)

    return {
        "status": card_status,
        "detail": card_detail,
    }


def reconcile_paper_live_transition_launch_block(
    *,
    paper_live_transition: dict[str, Any],
    operator_queue: dict[str, Any],
    first_launch_blocker: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile launch-blocker truth between transition cache and operator queue."""

    transition_launch_blocked_raw = paper_live_transition.get("operator_queue_launch_blocked_count")
    if transition_launch_blocked_raw is None:
        transition_launch_blocked_raw = operator_queue.get("launch_blocked_count")
    try:
        transition_launch_blocked = int(transition_launch_blocked_raw or 0)
    except (TypeError, ValueError):
        transition_launch_blocked = 0

    transition_first_launch_blocker = ""
    transition_first_launch_next_action = ""
    if transition_launch_blocked > 0:
        transition_first_launch_blocker = str(
            paper_live_transition.get("operator_queue_first_launch_blocker_op_id")
            or paper_live_transition.get("operator_queue_first_blocker_op_id")
            or ""
        )
        transition_first_launch_next_action = str(
            paper_live_transition.get("operator_queue_first_launch_next_action")
            or paper_live_transition.get("operator_queue_first_next_action")
            or ""
        )
        fresh_operator_queue = not operator_queue.get("cache_stale")
        if fresh_operator_queue and not transition_first_launch_blocker:
            transition_first_launch_blocker = str(first_launch_blocker.get("op_id") or "")
        if fresh_operator_queue and not transition_first_launch_next_action:
            launch_actions = first_launch_blocker.get("next_actions")
            if isinstance(launch_actions, list) and launch_actions:
                transition_first_launch_next_action = str(launch_actions[0])
            else:
                transition_first_launch_next_action = str(
                    first_launch_blocker.get("detail")
                    or first_launch_blocker.get("title")
                    or transition_first_launch_next_action
                )
        if paper_live_transition.get("cache_stale") and fresh_operator_queue:
            transition_first_launch_blocker = str(first_launch_blocker.get("op_id") or "")
            launch_actions = first_launch_blocker.get("next_actions")
            if isinstance(launch_actions, list) and launch_actions:
                transition_first_launch_next_action = str(launch_actions[0])
            else:
                transition_first_launch_next_action = str(
                    first_launch_blocker.get("detail")
                    or first_launch_blocker.get("title")
                    or transition_first_launch_next_action
                )

    return {
        "launch_blocked_count": transition_launch_blocked,
        "first_launch_blocker_op_id": transition_first_launch_blocker,
        "first_launch_next_action": transition_first_launch_next_action,
    }
