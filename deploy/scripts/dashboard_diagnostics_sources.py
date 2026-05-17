"""Shared diagnostics source-normalization helpers for the ETA dashboard."""

from __future__ import annotations

from typing import Any


def build_dashboard_diagnostics_bot_fleet_counts(
    *,
    roster: dict[str, Any],
    roster_summary: dict[str, Any],
) -> dict[str, int]:
    """Normalize bot-fleet count rollups for dashboard diagnostics."""

    roster_bots = roster.get("bots") if isinstance(roster.get("bots"), list) else []
    bot_total = int(roster_summary.get("bot_total") or len(roster_bots) or 0)
    confirmed_bots = int(roster.get("confirmed_bots") or roster_summary.get("confirmed_bots") or 0)
    active_bots = int(roster_summary.get("active_bots") or roster.get("active_bots") or 0)
    runtime_active_bots = int(
        roster_summary.get("runtime_active_bots")
        or roster.get("runtime_active_bots")
        or roster_summary.get("active_bots")
        or roster.get("active_bots")
        or 0
    )
    running_bots = int(roster_summary.get("running_bots") or 0)
    staged_bots = int(roster_summary.get("staged_bots") or roster.get("staged_bots") or 0)
    live_attached_bots = int(
        roster_summary.get("live_attached_bots")
        or roster.get("live_attached_bots")
        or runtime_active_bots
        or active_bots
        or 0
    )
    live_in_trade_bots = int(
        roster_summary.get("live_in_trade_bots")
        or roster.get("live_in_trade_bots")
        or running_bots
        or 0
    )

    idle_live_bots_raw = roster_summary.get("idle_live_bots")
    if idle_live_bots_raw is None:
        idle_live_bots_raw = roster.get("idle_live_bots")
    try:
        idle_live_bots = (
            int(idle_live_bots_raw)
            if idle_live_bots_raw is not None
            else max(0, live_attached_bots - live_in_trade_bots)
        )
    except (TypeError, ValueError):
        idle_live_bots = max(0, live_attached_bots - live_in_trade_bots)

    inactive_runtime_bots_raw = roster_summary.get("inactive_runtime_bots")
    if inactive_runtime_bots_raw is None:
        inactive_runtime_bots_raw = roster.get("inactive_runtime_bots")
    try:
        inactive_runtime_bots = (
            int(inactive_runtime_bots_raw)
            if inactive_runtime_bots_raw is not None
            else max(0, bot_total - live_attached_bots - staged_bots)
        )
    except (TypeError, ValueError):
        inactive_runtime_bots = max(0, bot_total - live_attached_bots - staged_bots)

    return {
        "bot_total": bot_total,
        "confirmed_bots": confirmed_bots,
        "active_bots": active_bots,
        "runtime_active_bots": runtime_active_bots,
        "running_bots": running_bots,
        "staged_bots": staged_bots,
        "live_attached_bots": live_attached_bots,
        "live_in_trade_bots": live_in_trade_bots,
        "idle_live_bots": idle_live_bots,
        "inactive_runtime_bots": inactive_runtime_bots,
    }


def extract_dashboard_operator_queue_rollups(
    *,
    operator_queue: dict[str, Any],
) -> dict[str, Any]:
    """Normalize operator-queue blocker and advisory rollups for diagnostics."""

    operator_summary = operator_queue.get("summary") if isinstance(operator_queue.get("summary"), dict) else {}
    top_operator_blockers = (
        operator_queue.get("top_blockers") if isinstance(operator_queue.get("top_blockers"), list) else []
    )
    top_launch_blockers = (
        operator_queue.get("top_launch_blockers")
        if isinstance(operator_queue.get("top_launch_blockers"), list)
        else []
    )
    first_operator_blocker = (
        top_operator_blockers[0] if top_operator_blockers and isinstance(top_operator_blockers[0], dict) else {}
    )
    first_launch_blocker = (
        top_launch_blockers[0] if top_launch_blockers and isinstance(top_launch_blockers[0], dict) else {}
    )
    first_operator_evidence = (
        first_operator_blocker.get("evidence") if isinstance(first_operator_blocker.get("evidence"), dict) else {}
    )
    first_operator_blocked_bots = first_operator_evidence.get("blocked_bots")
    if not isinstance(first_operator_blocked_bots, list):
        first_operator_blocked_bots = []
    first_operator_next_actions = first_operator_blocker.get("next_actions")
    if not isinstance(first_operator_next_actions, list):
        first_operator_next_actions = []

    top_operator_advisories = (
        operator_queue.get("top_non_launch_blockers")
        if isinstance(operator_queue.get("top_non_launch_blockers"), list)
        else []
    )
    first_operator_advisory = (
        top_operator_advisories[0]
        if top_operator_advisories and isinstance(top_operator_advisories[0], dict)
        else {}
    )
    first_operator_advisory_evidence = (
        first_operator_advisory.get("evidence")
        if isinstance(first_operator_advisory.get("evidence"), dict)
        else {}
    )
    first_operator_advisory_blocked_bots = first_operator_advisory_evidence.get("blocked_bots")
    if not isinstance(first_operator_advisory_blocked_bots, list):
        first_operator_advisory_blocked_bots = []
    first_operator_advisory_next_actions = first_operator_advisory.get("next_actions")
    if not isinstance(first_operator_advisory_next_actions, list):
        first_operator_advisory_next_actions = []

    return {
        "operator_summary": operator_summary,
        "first_operator_blocker": first_operator_blocker,
        "first_launch_blocker": first_launch_blocker,
        "first_operator_evidence": first_operator_evidence,
        "first_operator_blocked_bots": first_operator_blocked_bots,
        "first_operator_next_actions": first_operator_next_actions,
        "first_operator_advisory": first_operator_advisory,
        "first_operator_advisory_evidence": first_operator_advisory_evidence,
        "first_operator_advisory_blocked_bots": first_operator_advisory_blocked_bots,
        "first_operator_advisory_next_actions": first_operator_advisory_next_actions,
    }
