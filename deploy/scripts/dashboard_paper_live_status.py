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
