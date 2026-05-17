"""Shared paper-live and operator-queue status helpers for the ETA dashboard."""

from __future__ import annotations

from typing import Any

_READY_PAPER_LIVE_STATUSES = frozenset(
    {
        "ready",
        "ready_to_launch_paper_live",
        "green",
    },
)


def _normalize_first_failed_gate(first_failed_gate: dict[str, Any] | None) -> dict[str, str]:
    """Normalize first-failed-gate payloads into stable string fields."""

    if not isinstance(first_failed_gate, dict):
        return {}
    return {
        "name": str(first_failed_gate.get("name") or ""),
        "detail": str(first_failed_gate.get("detail") or ""),
        "next_action": str(first_failed_gate.get("next_action") or ""),
    }


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


def build_paper_live_transition_summary(
    *,
    raw_status: str,
    detail: str,
    stale_receipt: bool,
    stale_detail: str,
    effective_status: str,
    effective_detail: str,
    held_by_bracket_audit: bool,
    held_by_daily_loss_stop: bool,
    daily_loss_gate_mode: str,
    daily_loss_advisory_active: bool,
    capital_lanes_held_by_daily_loss_stop: bool,
    daily_loss_suppressed_non_authoritative_gateway_host: bool,
    broker_bracket_missing_count: int,
    broker_bracket_primary_symbol: str,
    broker_bracket_primary_venue: str,
    broker_bracket_primary_sec_type: str,
    paper_ready_bots: int,
    critical_ready: bool,
    operator_queue_launch_blocked_count: int,
    first_launch_blocker_op_id: str,
    first_launch_next_action: str,
    non_authoritative_gateway_host: bool,
    cache_stale: bool,
    error: object,
    first_failed_gate: dict[str, Any],
) -> dict[str, Any]:
    """Build the diagnostics `paper_live_transition` payload."""

    resolved_detail = str(detail or "").strip()
    if not resolved_detail:
        resolved_detail = str(
            first_launch_next_action
            or first_failed_gate.get("detail")
            or first_failed_gate.get("next_action")
            or ""
        ).strip()

    return {
        "status": str(raw_status or "unknown"),
        "detail": resolved_detail,
        "stale_receipt": bool(stale_receipt),
        "stale_detail": str(stale_detail or ""),
        "effective_status": str(effective_status or "unknown"),
        "effective_detail": str(effective_detail or ""),
        "held_by_bracket_audit": bool(held_by_bracket_audit),
        "held_by_daily_loss_stop": bool(held_by_daily_loss_stop),
        "daily_loss_gate_mode": str(daily_loss_gate_mode or ""),
        "daily_loss_advisory_active": bool(daily_loss_advisory_active),
        "capital_lanes_held_by_daily_loss_stop": bool(capital_lanes_held_by_daily_loss_stop),
        "daily_loss_suppressed_non_authoritative_gateway_host": bool(
            daily_loss_suppressed_non_authoritative_gateway_host
        ),
        "broker_bracket_missing_count": int(broker_bracket_missing_count or 0),
        "broker_bracket_primary_symbol": str(broker_bracket_primary_symbol or ""),
        "broker_bracket_primary_venue": str(broker_bracket_primary_venue or ""),
        "broker_bracket_primary_sec_type": str(broker_bracket_primary_sec_type or ""),
        "paper_ready_bots": int(paper_ready_bots or 0),
        "critical_ready": bool(critical_ready),
        "operator_queue_launch_blocked_count": int(operator_queue_launch_blocked_count or 0),
        "first_launch_blocker_op_id": str(first_launch_blocker_op_id or ""),
        "first_launch_next_action": str(first_launch_next_action or ""),
        "non_authoritative_gateway_host": bool(non_authoritative_gateway_host),
        "cache_stale": bool(cache_stale),
        "error": error,
        "first_failed_gate": first_failed_gate if isinstance(first_failed_gate, dict) else {},
    }


def build_operator_queue_diagnostics_summary(
    *,
    operator_summary: dict[str, Any],
    operator_queue: dict[str, Any],
    first_operator_blocker: dict[str, Any],
    first_operator_evidence: dict[str, Any],
    first_operator_blocked_bots: list[Any],
    first_operator_next_actions: list[Any],
    first_launch_blocker: dict[str, Any],
    first_operator_advisory: dict[str, Any],
    first_operator_advisory_evidence: dict[str, Any],
    first_operator_advisory_blocked_bots: list[Any],
    first_operator_advisory_next_actions: list[Any],
) -> dict[str, Any]:
    """Build the diagnostics `operator_queue` payload."""

    operator_advisory_count_raw = operator_queue.get("advisory_count")
    if operator_advisory_count_raw is None:
        operator_advisory_count_raw = operator_queue.get("non_launch_blocked_count")
    if operator_advisory_count_raw is None:
        operator_advisory_count_raw = max(0, int(operator_summary.get("BLOCKED") or 0))
    try:
        operator_advisory_count = int(operator_advisory_count_raw or 0)
    except (TypeError, ValueError):
        operator_advisory_count = 0

    launch_blocked_count = int(operator_queue.get("launch_blocked_count") or 0)

    return {
        "blocked": int(operator_summary.get("BLOCKED") or 0),
        "observed": int(operator_summary.get("OBSERVED") or 0),
        "unknown": int(operator_summary.get("UNKNOWN") or 0),
        "launch_blocked": launch_blocked_count,
        "advisory_count": operator_advisory_count,
        "advisory_only": bool(operator_queue.get("advisory_only"))
        or (operator_advisory_count > 0 and launch_blocked_count == 0),
        "top_blocker_op_id": str(first_operator_blocker.get("op_id") or ""),
        "top_blocker_title": str(first_operator_blocker.get("title") or ""),
        "top_blocker_detail": str(first_operator_blocker.get("detail") or ""),
        "top_blocker_launch_blocker": bool(first_operator_evidence.get("launch_blocker")),
        "top_blocker_launch_role": str(first_operator_evidence.get("launch_role") or ""),
        "top_blocker_blocked_bots": [str(bot) for bot in first_operator_blocked_bots],
        "top_blocker_next_actions": [str(action) for action in first_operator_next_actions],
        "top_launch_blocker_op_id": str(first_launch_blocker.get("op_id") or ""),
        "top_launch_blocker_detail": str(
            first_launch_blocker.get("detail") or first_launch_blocker.get("title") or ""
        ),
        "top_advisory_op_id": str(first_operator_advisory.get("op_id") or ""),
        "top_advisory_title": str(first_operator_advisory.get("title") or ""),
        "top_advisory_detail": str(first_operator_advisory.get("detail") or ""),
        "top_advisory_launch_role": str(first_operator_advisory_evidence.get("launch_role") or ""),
        "top_advisory_blocked_bots": [str(bot) for bot in first_operator_advisory_blocked_bots],
        "top_advisory_next_actions": [str(action) for action in first_operator_advisory_next_actions],
        "source": str(operator_queue.get("source") or "unknown"),
        "cache_status": str(operator_queue.get("cache_status") or ""),
        "cache_age_s": operator_queue.get("cache_age_s"),
        "cache_stale": bool(operator_queue.get("cache_stale")),
        "stale_cache_age_s": operator_queue.get("stale_cache_age_s"),
        "stale_cache_path": operator_queue.get("stale_cache_path"),
        "error": operator_queue.get("error"),
    }


def build_dashboard_paper_live_transition_diagnostics_summary(
    *,
    roster_summary: dict[str, Any],
    paper_live_transition: dict[str, Any],
    operator_queue: dict[str, Any],
    first_launch_blocker: dict[str, Any],
    first_failed_gate: dict[str, Any],
    default_daily_loss_gate_mode: str,
    daily_loss_shadow_detail: str,
    daily_loss_hold_detail: str,
) -> dict[str, Any]:
    """Build the diagnostics `paper_live_transition` rollup from raw inputs."""

    normalized_first_failed_gate = _normalize_first_failed_gate(first_failed_gate)

    transition_launch = reconcile_paper_live_transition_launch_block(
        paper_live_transition=paper_live_transition,
        operator_queue=operator_queue,
        first_launch_blocker=first_launch_blocker,
    )
    transition_launch_blocked = int(transition_launch["launch_blocked_count"] or 0)
    transition_first_launch_blocker = str(transition_launch["first_launch_blocker_op_id"] or "")
    transition_first_launch_next_action = str(transition_launch["first_launch_next_action"] or "")

    paper_live_status = str(paper_live_transition.get("status") or "unknown")
    paper_live_effective_status = str(
        roster_summary.get("paper_live_effective_status")
        or paper_live_transition.get("effective_status")
        or paper_live_status
    )
    paper_live_detail = str(paper_live_transition.get("detail") or "")
    paper_live_effective_detail = str(
        roster_summary.get("paper_live_effective_detail") or paper_live_transition.get("effective_detail") or ""
    )
    paper_live_held_by_bracket_audit = bool(
        roster_summary.get("paper_live_held_by_bracket_audit") or paper_live_transition.get("held_by_bracket_audit")
    )
    paper_live_daily_loss_gate_mode = str(
        roster_summary.get("paper_live_daily_loss_gate_mode")
        or paper_live_transition.get("daily_loss_gate_mode")
        or default_daily_loss_gate_mode
    )
    paper_live_daily_loss_advisory_active = bool(
        roster_summary.get("paper_live_daily_loss_advisory_active")
        or paper_live_transition.get("daily_loss_advisory_active")
    )
    paper_live_held_by_daily_loss_stop = bool(
        roster_summary.get("paper_live_held_by_daily_loss_stop") or paper_live_transition.get("held_by_daily_loss_stop")
    )
    paper_live_capital_lanes_held_by_daily_loss_stop = bool(
        roster_summary.get("paper_live_capital_lanes_held_by_daily_loss_stop")
        or paper_live_transition.get("capital_lanes_held_by_daily_loss_stop")
        or paper_live_held_by_daily_loss_stop
        or paper_live_daily_loss_advisory_active
    )
    paper_live_stale_receipt = bool(paper_live_transition.get("stale_receipt"))
    paper_live_stale_detail = str(paper_live_transition.get("stale_detail") or "")
    paper_live_effective = resolve_paper_live_effective_state(
        raw_status=paper_live_effective_status,
        effective_detail=paper_live_effective_detail,
        operator_queue_launch_blocked_count=transition_launch_blocked,
        operator_queue_blocked_detail=(
            transition_first_launch_next_action
            or str(first_launch_blocker.get("detail") or first_launch_blocker.get("title") or "")
            or "Fresh operator queue has a launch blocker."
        ),
        stale_receipt=paper_live_stale_receipt,
        stale_detail=paper_live_stale_detail,
        held_by_bracket_audit=False,
        bracket_audit_detail="",
        held_by_daily_loss_stop=paper_live_held_by_daily_loss_stop,
        daily_loss_advisory_active=paper_live_daily_loss_advisory_active,
        daily_loss_shadow_detail=daily_loss_shadow_detail,
        daily_loss_hold_detail=daily_loss_hold_detail,
    )

    return build_paper_live_transition_summary(
        raw_status=paper_live_status,
        detail=paper_live_detail,
        stale_receipt=paper_live_stale_receipt,
        stale_detail=paper_live_stale_detail,
        effective_status=str(paper_live_effective["effective_status"]),
        effective_detail=str(paper_live_effective["effective_detail"]),
        held_by_bracket_audit=paper_live_held_by_bracket_audit,
        held_by_daily_loss_stop=paper_live_held_by_daily_loss_stop,
        daily_loss_gate_mode=paper_live_daily_loss_gate_mode,
        daily_loss_advisory_active=paper_live_daily_loss_advisory_active,
        capital_lanes_held_by_daily_loss_stop=paper_live_capital_lanes_held_by_daily_loss_stop,
        daily_loss_suppressed_non_authoritative_gateway_host=bool(
            paper_live_transition.get("daily_loss_suppressed_non_authoritative_gateway_host")
        ),
        broker_bracket_missing_count=int(roster_summary.get("broker_bracket_missing_count") or 0),
        broker_bracket_primary_symbol=str(roster_summary.get("broker_bracket_primary_symbol") or ""),
        broker_bracket_primary_venue=str(roster_summary.get("broker_bracket_primary_venue") or ""),
        broker_bracket_primary_sec_type=str(roster_summary.get("broker_bracket_primary_sec_type") or ""),
        paper_ready_bots=int(paper_live_transition.get("paper_ready_bots") or 0),
        critical_ready=bool(paper_live_transition.get("critical_ready")),
        operator_queue_launch_blocked_count=transition_launch_blocked,
        first_launch_blocker_op_id=transition_first_launch_blocker,
        first_launch_next_action=transition_first_launch_next_action,
        non_authoritative_gateway_host=bool(paper_live_transition.get("non_authoritative_gateway_host")),
        cache_stale=bool(paper_live_transition.get("cache_stale")),
        error=paper_live_transition.get("error"),
        first_failed_gate=normalized_first_failed_gate,
    )


def build_master_status_paper_live_state(
    *,
    paper: dict[str, Any],
    runtime_mode: str,
    paper_ready: bool,
    blocked: int,
    launch_blocked: int,
    broker_bracket_prop_dry_run_blocked: bool,
    broker_bracket_action_labels: list[str],
    broker_bracket_effective_detail: str,
    daily_loss_killswitch: dict[str, Any],
    paper_live_lane_state: dict[str, Any],
    first_failed_gate: dict[str, Any] | None,
    daily_loss_shadow_detail: str,
    daily_loss_hold_detail: str,
    shadow_runtime_active: bool = False,
    shadow_runtime_detail: str = "",
) -> dict[str, Any]:
    """Build the shared paper-live runtime payload and card state for master-status."""

    paper_live = dict(paper)
    paper_live.update(
        {
            "mode": runtime_mode,
            "status": paper.get("status") or "unknown",
            "critical_ready": paper_ready,
            "paper_ready_bots": int(paper.get("paper_ready_bots") or 0),
            "operator_queue_blocked_count": blocked,
            "operator_queue_launch_blocked_count": launch_blocked,
        }
    )
    paper_held_by_bracket_audit = broker_bracket_prop_dry_run_blocked and paper_ready and launch_blocked == 0
    paper_held_by_daily_loss_stop = bool(paper_live_lane_state.get("held_by_daily_loss_stop")) and paper_ready and (
        launch_blocked == 0
    )
    paper_daily_loss_advisory_active = (
        bool(paper_live_lane_state.get("daily_loss_advisory_active"))
        and paper_ready
        and launch_blocked == 0
    )
    paper_non_authoritative_gateway_host = bool(paper.get("non_authoritative_gateway_host"))
    normalized_first_failed_gate = _normalize_first_failed_gate(first_failed_gate)
    paper_first_launch_next_action = str(
        paper.get("operator_queue_first_launch_next_action") or paper.get("operator_queue_first_next_action") or ""
    ).strip()
    paper_stale_receipt = bool(paper.get("stale_receipt"))
    paper_stale_detail = str(paper.get("stale_detail") or "")
    paper_live_effective = resolve_paper_live_effective_state(
        raw_status=str(paper.get("status") or "unknown"),
        effective_detail=str(paper.get("effective_detail") or ""),
        operator_queue_launch_blocked_count=launch_blocked,
        operator_queue_blocked_detail=paper_first_launch_next_action,
        stale_receipt=paper_stale_receipt,
        stale_detail=paper_stale_detail,
        held_by_bracket_audit=paper_held_by_bracket_audit,
        bracket_audit_detail=(
            f"held by Bracket Audit: {' or '.join(broker_bracket_action_labels)}"
            if broker_bracket_action_labels
            else str(broker_bracket_effective_detail or "held by Bracket Audit")
        ),
        held_by_daily_loss_stop=paper_held_by_daily_loss_stop,
        daily_loss_advisory_active=paper_daily_loss_advisory_active,
        daily_loss_shadow_detail=daily_loss_shadow_detail,
        daily_loss_hold_detail=daily_loss_hold_detail,
        shadow_runtime_active=shadow_runtime_active,
        shadow_runtime_detail=shadow_runtime_detail,
    )
    paper_live_effective_status = str(paper_live_effective["effective_status"])
    paper_live_effective_detail = str(paper_live_effective["effective_detail"])
    paper_live.update(
        {
            "raw_status": str(paper.get("status") or "unknown"),
            "effective_status": paper_live_effective_status,
            "effective_detail": paper_live_effective_detail,
            "stale_receipt": paper_stale_receipt,
            "stale_detail": paper_stale_detail,
            "non_authoritative_gateway_host": paper_non_authoritative_gateway_host,
            "first_launch_next_action": paper_first_launch_next_action,
            "held_by_bracket_audit": paper_held_by_bracket_audit,
            "held_by_daily_loss_stop": paper_held_by_daily_loss_stop,
            "daily_loss_gate_mode": str(paper_live_lane_state.get("gate_mode") or ""),
            "daily_loss_advisory_active": paper_daily_loss_advisory_active,
            "capital_lanes_held_by_daily_loss_stop": bool(
                paper_live_lane_state.get("capital_lanes_held_by_daily_loss_stop")
            ),
            "daily_loss_killswitch": daily_loss_killswitch,
        }
    )
    paper_card = resolve_paper_live_card(
        effective_status=paper_live_effective_status,
        stale_receipt=paper_stale_receipt,
        stale_detail=paper_stale_detail,
        non_authoritative_gateway_host=paper_non_authoritative_gateway_host,
        launch_blocked_count=launch_blocked,
        held_by_bracket_audit=paper_held_by_bracket_audit,
        held_by_daily_loss_stop=paper_held_by_daily_loss_stop,
        daily_loss_advisory_active=paper_daily_loss_advisory_active,
        critical_ready=paper_ready,
        shadow_runtime_active=shadow_runtime_active,
        blocked_detail=str(
            paper_first_launch_next_action
            or normalized_first_failed_gate.get("next_action")
            or paper.get("operator_queue_first_launch_next_action")
            or normalized_first_failed_gate.get("name")
            or paper_live_effective_status
        ),
    )
    return {
        "paper_live": paper_live,
        "paper_card": paper_card,
    }
