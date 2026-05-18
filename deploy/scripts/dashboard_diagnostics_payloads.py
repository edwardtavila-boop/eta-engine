"""Shared diagnostics payload builders for the ETA dashboard."""

from __future__ import annotations

from typing import Any


def build_dashboard_diagnostics_readiness_payload(
    *,
    readiness: dict[str, Any],
    readiness_summary: dict[str, Any],
    readiness_lane_counts: dict[str, Any],
    readiness_blocked_data: int,
) -> dict[str, Any]:
    """Build the diagnostics `bot_strategy_readiness` payload."""

    return {
        "status": str(readiness.get("status") or "unknown"),
        "blocked_data": int(readiness_blocked_data or 0),
        "paper_ready": int(readiness_summary.get("can_paper_trade") or 0),
        "can_live_any": bool(readiness_summary.get("can_live_any")),
        "launch_lanes": readiness_lane_counts if isinstance(readiness_lane_counts, dict) else {},
        "top_action_count": len(readiness.get("top_actions") or []),
        "error": readiness.get("error"),
    }


def build_dashboard_diagnostics_second_brain_payload(
    *,
    second_brain: dict[str, Any],
    eligible_patterns: int,
    favor_pattern_count: int,
    avoid_pattern_count: int,
    truth_note: str,
) -> dict[str, Any]:
    """Build the diagnostics `second_brain` payload."""

    return {
        "status": str(second_brain.get("status") or "unknown"),
        "n_episodes": int(second_brain.get("n_episodes") or 0),
        "win_rate": second_brain.get("win_rate"),
        "avg_r": second_brain.get("avg_r"),
        "semantic_patterns": int(second_brain.get("semantic_patterns") or 0),
        "procedural_versions": int(second_brain.get("procedural_versions") or 0),
        "eligible_patterns": int(eligible_patterns or 0),
        "favor_pattern_count": int(favor_pattern_count or 0),
        "avoid_pattern_count": int(avoid_pattern_count or 0),
        "legacy_sources_active": bool(second_brain.get("legacy_sources_active")),
        "sources": second_brain.get("sources") if isinstance(second_brain.get("sources"), dict) else {},
        "paths": second_brain.get("paths") if isinstance(second_brain.get("paths"), dict) else {},
        "truth_note": str(truth_note or ""),
        "error": second_brain.get("error"),
    }


def build_dashboard_diagnostics_equity_payload(
    *,
    equity: dict[str, Any],
    equity_series: list[Any],
    equity_summary: dict[str, Any],
) -> dict[str, Any]:
    """Build the diagnostics `equity` payload."""

    return {
        "source": str(equity.get("source") or "unknown"),
        "session_truth_status": str(equity.get("session_truth_status") or "unknown"),
        "source_age_s": equity.get("source_age_s"),
        "point_count": len(equity_series) if isinstance(equity_series, list) else 0,
        "today_pnl": equity_summary.get("today_pnl") if isinstance(equity_summary, dict) else None,
        "error": equity.get("_error"),
    }


def build_dashboard_diagnostics_dirty_worktree_payload(
    *,
    dirty_worktree_reconciliation: dict[str, Any],
) -> dict[str, Any]:
    """Build the diagnostics `dirty_worktree_reconciliation` payload."""

    return {
        "status": str(dirty_worktree_reconciliation.get("status") or "unknown"),
        "ready": bool(dirty_worktree_reconciliation.get("ready")),
        "action": str(dirty_worktree_reconciliation.get("action") or ""),
        "dirty_modules": dirty_worktree_reconciliation.get("dirty_modules")
        if isinstance(dirty_worktree_reconciliation.get("dirty_modules"), list)
        else [],
        "blocking_modules": dirty_worktree_reconciliation.get("blocking_modules")
        if isinstance(dirty_worktree_reconciliation.get("blocking_modules"), list)
        else [],
        "next_actions": dirty_worktree_reconciliation.get("next_actions")
        if isinstance(dirty_worktree_reconciliation.get("next_actions"), list)
        else [],
        "module_summaries": dirty_worktree_reconciliation.get("module_summaries")
        if isinstance(dirty_worktree_reconciliation.get("module_summaries"), list)
        else [],
        "review_batches": dirty_worktree_reconciliation.get("review_batches")
        if isinstance(dirty_worktree_reconciliation.get("review_batches"), list)
        else [],
        "error": dirty_worktree_reconciliation.get("error"),
    }


def build_dashboard_diagnostics_paper_live_payload(
    *,
    paper_live_transition_summary: dict[str, Any],
    operator_summary: dict[str, Any],
    paper_live_transition: dict[str, Any],
) -> dict[str, Any]:
    """Build the diagnostics `paper_live_transition` payload."""

    blocked_raw = (operator_summary if isinstance(operator_summary, dict) else {}).get("BLOCKED")
    try:
        blocked_count = int(blocked_raw or 0)
    except (TypeError, ValueError):
        blocked_count = 0

    return {
        **(paper_live_transition_summary if isinstance(paper_live_transition_summary, dict) else {}),
        "operator_queue_blocked_count": blocked_count,
        "source_age_s": (
            paper_live_transition.get("source_age_s") if isinstance(paper_live_transition, dict) else None
        ),
    }


def build_dashboard_command_center_watchdog_summary_payload(
    *,
    command_center_watchdog: dict[str, Any],
    eta_readiness_snapshot: dict[str, Any],
    roster_summary: dict[str, Any] | None = None,
    apply_readiness_stale_overrides: bool = False,
) -> dict[str, Any]:
    """Build the shared command-center watchdog summary payload."""

    watchdog = command_center_watchdog if isinstance(command_center_watchdog, dict) else {}
    readiness = eta_readiness_snapshot if isinstance(eta_readiness_snapshot, dict) else {}
    roster = roster_summary if isinstance(roster_summary, dict) else {}
    readiness_issue_status = str(readiness.get("command_center_issue_status") or "").strip()
    readiness_issue_active = (
        apply_readiness_stale_overrides and bool(readiness_issue_status) and readiness_issue_status != "healthy"
    )
    instruction_next_step = str(
        readiness.get("command_center_operator_next_step")
        or roster.get("command_center_watchdog_next_step")
        or watchdog.get("operator_next_step")
        or watchdog.get("next_step")
        or ""
    ).strip()
    action_plan = (
        list(roster.get("command_center_watchdog_action_plan"))
        if isinstance(roster.get("command_center_watchdog_action_plan"), list)
        else (list(watchdog.get("action_plan")) if isinstance(watchdog.get("action_plan"), list) else [])
    )
    follow_up_actions = (
        list(roster.get("command_center_watchdog_follow_up_actions"))
        if isinstance(roster.get("command_center_watchdog_follow_up_actions"), list)
        else (list(watchdog.get("follow_up_actions")) if isinstance(watchdog.get("follow_up_actions"), list) else [])
    )
    return {
        "command_center_watchdog_summary_line": str(
            roster.get("command_center_watchdog_summary_line")
            or watchdog.get("summary_line")
            or watchdog.get("summary")
            or watchdog.get("display_summary")
            or watchdog.get("display_issue_summary")
            or watchdog.get("issue_summary")
            or readiness.get("command_center_issue_summary")
            or ""
        ),
        "command_center_watchdog_issue_status": str(
            readiness.get("command_center_issue_status")
            or roster.get("command_center_watchdog_issue_status")
            or watchdog.get("issue_status")
            or watchdog.get("status")
            or ""
        ),
        "command_center_watchdog_operator_next_step": str(
            roster.get("command_center_watchdog_operator_next_step")
            or watchdog.get("operator_next_step")
            or watchdog.get("next_step")
            or ""
        ),
        "command_center_watchdog_operator_next_reason": str(
            roster.get("command_center_watchdog_operator_next_reason")
            or watchdog.get("operator_next_reason")
            or watchdog.get("next_reason")
            or ""
        ),
        "command_center_watchdog_operator_next_command": (
            roster.get("command_center_watchdog_operator_next_command")
            if roster.get("command_center_watchdog_operator_next_command") is not None
            else watchdog.get("operator_next_command")
        ),
        "command_center_watchdog_operator_next_requires_elevation": (
            roster.get("command_center_watchdog_operator_next_requires_elevation")
            if roster.get("command_center_watchdog_operator_next_requires_elevation") is not None
            else watchdog.get("operator_next_requires_elevation")
        ),
        "command_center_watchdog_next_step": str(
            readiness.get("command_center_operator_next_step")
            or roster.get("command_center_watchdog_next_step")
            or watchdog.get("operator_next_step")
            or watchdog.get("next_step")
            or ""
        ),
        "command_center_watchdog_next_reason": str(
            readiness.get("command_center_operator_next_reason")
            or roster.get("command_center_watchdog_next_reason")
            or watchdog.get("operator_next_reason")
            or watchdog.get("next_reason")
            or ""
        ),
        "command_center_watchdog_next_command": str(
            readiness.get("command_center_operator_next_command")
            or roster.get("command_center_watchdog_next_command")
            or watchdog.get("operator_next_command")
            or watchdog.get("next_command")
            or ""
        ),
        "command_center_watchdog_failure_class": str(
            ("stale_service" if readiness_issue_active else "")
            or roster.get("command_center_watchdog_failure_class")
            or watchdog.get("failure_class")
            or ""
        ),
        "command_center_watchdog_operator_contract_state": str(
            ("stale_service" if readiness_issue_active else "")
            or roster.get("command_center_watchdog_operator_contract_state")
            or watchdog.get("operator_contract_state")
            or ""
        ),
        "command_center_watchdog_recommended_action": str(
            readiness.get("command_center_operator_next_step")
            or roster.get("command_center_watchdog_recommended_action")
            or watchdog.get("recommended_action")
            or ""
        ),
        "command_center_watchdog_primary_blocker": str(
            readiness.get("command_center_issue_status")
            or roster.get("command_center_watchdog_primary_blocker")
            or watchdog.get("primary_blocker")
            or watchdog.get("issue_status")
            or watchdog.get("status")
            or ""
        ),
        "command_center_watchdog_instruction": str(
            roster.get("command_center_watchdog_instruction")
            or watchdog.get("instruction")
            or watchdog.get("operator_next_instruction")
            or (
                "Run the launcher and approve the UAC prompt."
                if instruction_next_step == "reload_operator_service"
                else ""
            )
            or ""
        ),
        "command_center_watchdog_action_plan": action_plan,
        "command_center_watchdog_action_count": int(
            2
            if readiness_issue_active
            else (
                roster.get("command_center_watchdog_action_count")
                if roster.get("command_center_watchdog_action_count") is not None
                else (watchdog.get("action_count") or 0)
            )
        ),
        "command_center_watchdog_follow_up_actions": follow_up_actions,
        "command_center_watchdog_follow_up_count": int(
            1
            if readiness_issue_active
            else (
                roster.get("command_center_watchdog_follow_up_count")
                if roster.get("command_center_watchdog_follow_up_count") is not None
                else (watchdog.get("follow_up_count") or 0)
            )
        ),
    }


def build_dashboard_diagnostics_retune_focus_payload(
    *,
    diamond_retune_status: dict[str, Any],
    diamond_retune_summary: dict[str, Any],
) -> dict[str, Any]:
    """Build the diagnostics retune-focus payload fields."""

    return {
        "retune_focus_bot_id": str(diamond_retune_status.get("focus_bot") or ""),
        "retune_focus_state": str(diamond_retune_status.get("focus_state") or ""),
        "retune_focus_issue": str(diamond_retune_status.get("focus_issue") or ""),
        "retune_focus_next_action": str(diamond_retune_status.get("focus_next_action") or ""),
        "retune_focus_active_experiment": (
            dict(diamond_retune_status.get("focus_active_experiment"))
            if isinstance(diamond_retune_status.get("focus_active_experiment"), dict)
            else {}
        ),
        "retune_focus_active_experiment_summary_line": str(
            diamond_retune_summary.get("broker_truth_focus_active_experiment_summary_line") or ""
        ),
        "retune_focus_active_experiment_outcome_line": str(
            diamond_retune_status.get("focus_active_experiment_outcome_line") or ""
        ),
    }


def build_dashboard_diagnostics_retune_payload(
    *,
    diamond_retune_status: dict[str, Any],
    diamond_retune_summary: dict[str, Any],
    eta_readiness_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Build the diagnostics retune truth payload cluster."""

    return {
        **build_dashboard_diagnostics_retune_focus_payload(
            diamond_retune_status=diamond_retune_status,
            diamond_retune_summary=diamond_retune_summary,
        ),
        "public_live_retune_generated_at_utc": str(
            eta_readiness_snapshot.get("public_live_retune_generated_at_utc") or ""
        ),
        "public_live_retune_focus_active_experiment_outcome_line": str(
            eta_readiness_snapshot.get("public_live_retune_focus_active_experiment_outcome_line") or ""
        ),
        "public_live_retune_sync_drift_display": str(
            eta_readiness_snapshot.get("public_live_retune_sync_drift_display") or ""
        ),
        "dashboard_api_runtime_public_live_retune_generated_at_utc": str(
            eta_readiness_snapshot.get("dashboard_api_runtime_public_live_retune_generated_at_utc") or ""
        ),
        "dashboard_api_runtime_public_live_retune_sync_drift_display": str(
            eta_readiness_snapshot.get("dashboard_api_runtime_public_live_retune_sync_drift_display") or ""
        ),
        "dashboard_api_runtime_retune_drift_display": str(
            eta_readiness_snapshot.get("dashboard_api_runtime_retune_drift_display") or ""
        ),
        "current_live_retune_generated_at_utc": str(
            eta_readiness_snapshot.get("current_live_retune_generated_at_utc") or ""
        ),
        "current_live_retune_focus_active_experiment_outcome_line": str(
            eta_readiness_snapshot.get("current_live_retune_focus_active_experiment_outcome_line") or ""
        ),
        "current_live_retune_sync_drift_display": str(
            eta_readiness_snapshot.get("current_live_retune_sync_drift_display") or ""
        ),
        "local_retune_generated_at_utc": str(eta_readiness_snapshot.get("local_retune_generated_at_utc") or ""),
        "local_retune_focus_active_experiment_outcome_line": str(
            eta_readiness_snapshot.get("local_retune_focus_active_experiment_outcome_line") or ""
        ),
        "retune_focus_active_experiment_drift_display": str(
            eta_readiness_snapshot.get("retune_focus_active_experiment_drift_display") or ""
        ),
        "current_local_retune_generated_at_utc": str(
            eta_readiness_snapshot.get("current_local_retune_generated_at_utc") or ""
        ),
        "local_retune_sync_drift_display": str(
            eta_readiness_snapshot.get("local_retune_sync_drift_display") or ""
        ),
    }


def _format_retune_experiment_currency(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def resolve_dashboard_retune_focus_active_experiment_outcome_line(
    experiment: dict[str, Any] | None,
    *,
    fallback: object = "",
) -> str:
    """Resolve the dashboard retune active-experiment outcome line."""

    fallback_text = "" if isinstance(fallback, bool) else str(fallback or "").strip()
    if fallback_text:
        return fallback_text
    if not isinstance(experiment, dict):
        return ""
    raw_experiment_id = experiment.get("experiment_id")
    experiment_id = "" if isinstance(raw_experiment_id, bool) else str(raw_experiment_id or "").strip()
    if not experiment_id:
        return ""
    if experiment.get("awaiting_first_post_change_close") is True:
        return f"{experiment_id}: awaiting first post-change close"

    raw_close_count = experiment.get("post_change_closed_trade_count")
    if isinstance(raw_close_count, bool):
        close_count = 0
    else:
        try:
            close_count = int(raw_close_count or 0)
        except (TypeError, ValueError):
            close_count = 0
    if close_count <= 0:
        return experiment_id

    parts = [f"{experiment_id}: {close_count} post-change close{'s' if close_count != 1 else ''}"]
    for raw_value, label, formatter in (
        (experiment.get("post_change_cumulative_r"), "R", lambda v: f"{v:+.2f}"),
        (experiment.get("post_change_total_realized_pnl"), "PnL", _format_retune_experiment_currency),
        (experiment.get("post_change_profit_factor"), "PF", lambda v: f"{v:.2f}"),
    ):
        if isinstance(raw_value, bool) or raw_value is None:
            continue
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            continue
        parts.append(f"{label} {formatter(numeric_value)}")
    return " | ".join(parts)


def build_dashboard_retune_focus_overlay_payload(
    *,
    snapshot: dict[str, Any],
    readiness_snapshot: dict[str, Any],
    focus_active_experiment_summary_line: str,
    focus_active_experiment_outcome_line: str,
) -> dict[str, Any]:
    """Build the shared retune overlay payload for bot-fleet rows and drilldowns."""

    return {
        "retune_focus_bot_id": str(snapshot.get("focus_bot") or ""),
        "retune_focus_state": str(snapshot.get("focus_state") or ""),
        "retune_focus_issue": str(snapshot.get("focus_issue") or ""),
        "retune_focus_strategy_kind": str(snapshot.get("focus_strategy_kind") or ""),
        "retune_focus_next_action": str(
            snapshot.get("focus_next_action") or snapshot.get("focus_next_command") or ""
        ),
        "retune_focus_active_experiment": (
            dict(snapshot.get("focus_active_experiment"))
            if isinstance(snapshot.get("focus_active_experiment"), dict)
            else {}
        ),
        "retune_focus_active_experiment_summary_line": str(focus_active_experiment_summary_line or ""),
        "retune_focus_active_experiment_outcome_line": str(focus_active_experiment_outcome_line or ""),
        "public_live_retune_focus_active_experiment_outcome_line": str(
            readiness_snapshot.get("public_live_retune_focus_active_experiment_outcome_line") or ""
        ),
        "current_live_retune_generated_at_utc": str(
            readiness_snapshot.get("current_live_retune_generated_at_utc") or ""
        ),
        "current_live_retune_focus_active_experiment_outcome_line": str(
            readiness_snapshot.get("current_live_retune_focus_active_experiment_outcome_line") or ""
        ),
        "current_live_retune_sync_drift_display": str(
            readiness_snapshot.get("current_live_retune_sync_drift_display") or ""
        ),
        "local_retune_focus_active_experiment_outcome_line": str(
            readiness_snapshot.get("local_retune_focus_active_experiment_outcome_line") or ""
        ),
        "retune_focus_active_experiment_drift_display": str(
            readiness_snapshot.get("retune_focus_active_experiment_drift_display") or ""
        ),
    }


def build_dashboard_retune_focus_summary_payload(
    *,
    snapshot: dict[str, Any],
    readiness_snapshot: dict[str, Any],
    focus_active_experiment_summary_line: str,
    focus_active_experiment_outcome_line: str,
) -> dict[str, Any]:
    """Build the bot-fleet summary retune-focus payload."""

    return {
        "retune_focus_bot_id": str(snapshot.get("focus_bot") or ""),
        "retune_focus_state": str(snapshot.get("focus_state") or ""),
        "retune_focus_issue": str(snapshot.get("focus_issue") or ""),
        "retune_focus_next_action": str(snapshot.get("focus_next_action") or ""),
        "retune_focus_active_experiment": (
            dict(snapshot.get("focus_active_experiment"))
            if isinstance(snapshot.get("focus_active_experiment"), dict)
            else {}
        ),
        "retune_focus_active_experiment_summary_line": str(focus_active_experiment_summary_line or ""),
        "retune_focus_active_experiment_outcome_line": str(focus_active_experiment_outcome_line or ""),
        "public_live_retune_focus_active_experiment_outcome_line": str(
            readiness_snapshot.get("public_live_retune_focus_active_experiment_outcome_line") or ""
        ),
        "local_retune_focus_active_experiment_outcome_line": str(
            readiness_snapshot.get("local_retune_focus_active_experiment_outcome_line") or ""
        ),
        "retune_focus_active_experiment_drift_display": str(
            readiness_snapshot.get("retune_focus_active_experiment_drift_display") or ""
        ),
    }


def build_dashboard_normalized_diamond_retune_status_payload(
    *,
    payload: dict[str, Any],
    path: str,
    focus_active_experiment_outcome_line: str,
) -> dict[str, Any]:
    """Build the normalized diamond retune snapshot payload."""

    raw_summary = payload.get("summary")
    raw_bots = payload.get("bots")
    raw_research_backlog = payload.get("research_backlog")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    bots = raw_bots if isinstance(raw_bots, list) else []
    research_backlog = raw_research_backlog if isinstance(raw_research_backlog, list) else []
    first_bot = bots[0] if bots and isinstance(bots[0], dict) else {}
    kind_ok = payload.get("kind") == "eta_diamond_retune_status"
    contract_ok = kind_ok and isinstance(raw_summary, dict) and isinstance(raw_bots, list)
    status = str(payload.get("status") or ("ready" if contract_ok else "invalid"))
    focus_active_experiment = (
        dict(summary.get("broker_truth_focus_active_experiment"))
        if isinstance(summary.get("broker_truth_focus_active_experiment"), dict)
        else (
            dict(payload.get("focus_active_experiment"))
            if isinstance(payload.get("focus_active_experiment"), dict)
            else {}
        )
    )
    normalized_summary = {
        "n_targets": int(summary.get("n_targets") or len(bots)),
        "n_attempted_bots": int(summary.get("n_attempted_bots") or 0),
        "n_unattempted_targets": int(summary.get("n_unattempted_targets") or 0),
        "n_research_backlog_targets": int(summary.get("n_research_backlog_targets") or len(research_backlog)),
        "n_low_sample_keep_collecting": int(summary.get("n_low_sample_keep_collecting") or 0),
        "n_near_miss_keep_tuning": int(summary.get("n_near_miss_keep_tuning") or 0),
        "n_unstable_positive_keep_tuning": int(summary.get("n_unstable_positive_keep_tuning") or 0),
        "n_research_passed_broker_proof_required": int(summary.get("n_research_passed_broker_proof_required") or 0),
        "n_stuck_research_failing": int(summary.get("n_stuck_research_failing") or 0),
        "n_timeout_retry": int(summary.get("n_timeout_retry") or 0),
        "broker_proof_required_closes": int(summary.get("broker_proof_required_closes") or 100),
        "n_broker_sample_ready": int(summary.get("n_broker_sample_ready") or 0),
        "n_broker_edge_ready": int(summary.get("n_broker_edge_ready") or 0),
        "n_broker_proof_ready": int(summary.get("n_broker_proof_ready") or 0),
        "n_broker_sample_ready_negative_edge": int(summary.get("n_broker_sample_ready_negative_edge") or 0),
        "n_broker_proof_shortfall": int(summary.get("n_broker_proof_shortfall") or 0),
        "largest_broker_proof_gap": int(summary.get("largest_broker_proof_gap") or 0),
        "total_broker_proof_gap": int(summary.get("total_broker_proof_gap") or 0),
        "broker_truth_focus_bot_id": str(summary.get("broker_truth_focus_bot_id") or ""),
        "broker_truth_focus_state": str(summary.get("broker_truth_focus_state") or ""),
        "broker_truth_focus_edge_status": str(summary.get("broker_truth_focus_edge_status") or ""),
        "broker_truth_focus_closed_trade_count": int(summary.get("broker_truth_focus_closed_trade_count") or 0),
        "broker_truth_focus_required_closed_trade_count": int(
            summary.get("broker_truth_focus_required_closed_trade_count") or 100
        ),
        "broker_truth_focus_remaining_closed_trade_count": int(
            summary.get("broker_truth_focus_remaining_closed_trade_count") or 0
        ),
        "broker_truth_focus_total_realized_pnl": float(summary.get("broker_truth_focus_total_realized_pnl") or 0.0),
        "broker_truth_focus_profit_factor": float(summary.get("broker_truth_focus_profit_factor") or 0.0),
        "broker_truth_focus_issue_code": str(summary.get("broker_truth_focus_issue_code") or ""),
        "broker_truth_focus_priority_score": float(summary.get("broker_truth_focus_priority_score") or 0.0),
        "broker_truth_focus_strategy_kind": str(summary.get("broker_truth_focus_strategy_kind") or ""),
        "broker_truth_focus_best_session": str(summary.get("broker_truth_focus_best_session") or ""),
        "broker_truth_focus_worst_session": str(summary.get("broker_truth_focus_worst_session") or ""),
        "broker_truth_focus_parameter_focus": (
            [str(item) for item in summary.get("broker_truth_focus_parameter_focus")]
            if isinstance(summary.get("broker_truth_focus_parameter_focus"), list)
            else []
        ),
        "broker_truth_focus_primary_experiment": str(summary.get("broker_truth_focus_primary_experiment") or ""),
        "broker_truth_focus_next_command": str(summary.get("broker_truth_focus_next_command") or ""),
        "broker_truth_focus_next_action": str(summary.get("broker_truth_focus_next_action") or ""),
        "broker_truth_focus_active_experiment": focus_active_experiment,
        "broker_truth_focus_active_experiment_summary_line": str(
            summary.get("broker_truth_focus_active_experiment_summary_line") or ""
        ),
        "broker_truth_focus_active_experiment_outcome_line": str(focus_active_experiment_outcome_line or ""),
        "broker_truth_summary_line": str(summary.get("broker_truth_summary_line") or ""),
        "safe_to_mutate_live": False,
    }
    focus_bot = normalized_summary["broker_truth_focus_bot_id"] or str(first_bot.get("bot_id") or "")
    focus_state = normalized_summary["broker_truth_focus_state"] or str(
        first_bot.get("retune_state") or first_bot.get("stage") or ""
    )
    normalized: dict[str, Any] = dict(payload)
    normalized.update(
        {
            "kind": str(payload.get("kind") or "eta_diamond_retune_status"),
            "source": str(payload.get("source") or "diamond_retune_status_latest"),
            "path": str(path),
            "source_path": str(path),
            "status": status,
            "ready": contract_ok,
            "contract_ok": contract_ok,
            "safe_to_mutate_live": False,
            "writes_live_routing": False,
            "summary": normalized_summary,
            "bots": bots,
            "research_backlog": research_backlog,
            "focus_bot": focus_bot,
            "focus_state": focus_state,
            "focus_issue": normalized_summary["broker_truth_focus_issue_code"],
            "focus_strategy_kind": normalized_summary["broker_truth_focus_strategy_kind"],
            "focus_best_session": normalized_summary["broker_truth_focus_best_session"],
            "focus_worst_session": normalized_summary["broker_truth_focus_worst_session"],
            "focus_parameter_focus": list(normalized_summary["broker_truth_focus_parameter_focus"]),
            "focus_command": normalized_summary["broker_truth_focus_next_command"],
            "focus_next_action": normalized_summary["broker_truth_focus_next_action"],
            "focus_active_experiment": dict(normalized_summary["broker_truth_focus_active_experiment"]),
            "focus_active_experiment_outcome_line": str(focus_active_experiment_outcome_line or ""),
        }
    )
    return normalized


def build_dashboard_unknown_diamond_retune_status_payload(
    *,
    path: str,
    reason: str,
) -> dict[str, Any]:
    """Build the fail-closed missing diamond retune snapshot payload."""

    return {
        "kind": "eta_diamond_retune_status",
        "source": str(reason or "missing_snapshot"),
        "path": str(path),
        "source_path": str(path),
        "status": "missing",
        "ready": False,
        "contract_ok": False,
        "safe_to_mutate_live": False,
        "summary": {
            "n_targets": 0,
            "n_attempted_bots": 0,
            "n_unattempted_targets": 0,
            "n_research_backlog_targets": 0,
            "n_low_sample_keep_collecting": 0,
            "n_near_miss_keep_tuning": 0,
            "n_unstable_positive_keep_tuning": 0,
            "n_research_passed_broker_proof_required": 0,
            "n_stuck_research_failing": 0,
            "n_timeout_retry": 0,
            "broker_proof_required_closes": 100,
            "n_broker_sample_ready": 0,
            "n_broker_edge_ready": 0,
            "n_broker_proof_ready": 0,
            "n_broker_sample_ready_negative_edge": 0,
            "n_broker_proof_shortfall": 0,
            "largest_broker_proof_gap": 0,
            "total_broker_proof_gap": 0,
            "broker_truth_focus_bot_id": "",
            "broker_truth_focus_state": "",
            "broker_truth_focus_edge_status": "",
            "broker_truth_focus_closed_trade_count": 0,
            "broker_truth_focus_required_closed_trade_count": 100,
            "broker_truth_focus_remaining_closed_trade_count": 0,
            "broker_truth_focus_total_realized_pnl": 0.0,
            "broker_truth_focus_profit_factor": 0.0,
            "broker_truth_focus_issue_code": "",
            "broker_truth_focus_priority_score": 0.0,
            "broker_truth_focus_strategy_kind": "",
            "broker_truth_focus_best_session": "",
            "broker_truth_focus_worst_session": "",
            "broker_truth_focus_parameter_focus": [],
            "broker_truth_focus_primary_experiment": "",
            "broker_truth_focus_next_command": "",
            "broker_truth_focus_next_action": "",
            "broker_truth_focus_active_experiment": {},
            "broker_truth_focus_active_experiment_summary_line": "",
            "broker_truth_focus_active_experiment_outcome_line": "",
            "broker_truth_summary_line": "",
            "safe_to_mutate_live": False,
        },
        "bots": [],
        "research_backlog": [],
        "notes": ["diamond retune status has not been generated"],
    }


def build_dashboard_diagnostics_diamond_retune_payload(
    *,
    snapshot: dict[str, Any],
    path: str,
    updated_at: str | None,
    age_s: int | None,
    broker_truth_focus_active_experiment_outcome_line: str = "",
) -> dict[str, Any]:
    """Build the diagnostics `diamond_retune_status` payload."""

    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    bots = snapshot.get("bots") if isinstance(snapshot.get("bots"), list) else []
    first_bot = bots[0] if bots and isinstance(bots[0], dict) else {}
    broker_evidence = (
        first_bot.get("broker_close_evidence") if isinstance(first_bot.get("broker_close_evidence"), dict) else {}
    )
    return {
        "status": str(snapshot.get("status") or "missing"),
        "ready": bool(snapshot.get("ready")),
        "contract_ok": bool(snapshot.get("contract_ok")),
        "n_targets": int(summary.get("n_targets") or len(bots)),
        "n_attempted_bots": int(summary.get("n_attempted_bots") or 0),
        "n_unattempted_targets": int(summary.get("n_unattempted_targets") or 0),
        "n_research_backlog_targets": int(summary.get("n_research_backlog_targets") or 0),
        "n_low_sample_keep_collecting": int(summary.get("n_low_sample_keep_collecting") or 0),
        "n_near_miss_keep_tuning": int(summary.get("n_near_miss_keep_tuning") or 0),
        "n_unstable_positive_keep_tuning": int(summary.get("n_unstable_positive_keep_tuning") or 0),
        "n_research_passed_broker_proof_required": int(
            summary.get("n_research_passed_broker_proof_required") or 0
        ),
        "n_stuck_research_failing": int(summary.get("n_stuck_research_failing") or 0),
        "n_timeout_retry": int(summary.get("n_timeout_retry") or 0),
        "broker_proof_required_closes": int(summary.get("broker_proof_required_closes") or 100),
        "n_broker_sample_ready": int(summary.get("n_broker_sample_ready") or 0),
        "n_broker_edge_ready": int(summary.get("n_broker_edge_ready") or 0),
        "n_broker_proof_ready": int(summary.get("n_broker_proof_ready") or 0),
        "n_broker_sample_ready_negative_edge": int(summary.get("n_broker_sample_ready_negative_edge") or 0),
        "n_broker_proof_shortfall": int(summary.get("n_broker_proof_shortfall") or 0),
        "largest_broker_proof_gap": int(summary.get("largest_broker_proof_gap") or 0),
        "total_broker_proof_gap": int(summary.get("total_broker_proof_gap") or 0),
        "broker_truth_focus_bot_id": str(summary.get("broker_truth_focus_bot_id") or ""),
        "broker_truth_focus_state": str(summary.get("broker_truth_focus_state") or ""),
        "broker_truth_focus_edge_status": str(summary.get("broker_truth_focus_edge_status") or ""),
        "broker_truth_focus_closed_trade_count": int(summary.get("broker_truth_focus_closed_trade_count") or 0),
        "broker_truth_focus_required_closed_trade_count": int(
            summary.get("broker_truth_focus_required_closed_trade_count") or 100
        ),
        "broker_truth_focus_remaining_closed_trade_count": int(
            summary.get("broker_truth_focus_remaining_closed_trade_count") or 0
        ),
        "broker_truth_focus_total_realized_pnl": float(summary.get("broker_truth_focus_total_realized_pnl") or 0.0),
        "broker_truth_focus_profit_factor": float(summary.get("broker_truth_focus_profit_factor") or 0.0),
        "broker_truth_focus_issue_code": str(summary.get("broker_truth_focus_issue_code") or ""),
        "broker_truth_focus_priority_score": float(summary.get("broker_truth_focus_priority_score") or 0.0),
        "broker_truth_focus_strategy_kind": str(summary.get("broker_truth_focus_strategy_kind") or ""),
        "broker_truth_focus_best_session": str(summary.get("broker_truth_focus_best_session") or ""),
        "broker_truth_focus_worst_session": str(summary.get("broker_truth_focus_worst_session") or ""),
        "broker_truth_focus_parameter_focus": (
            [str(item) for item in summary.get("broker_truth_focus_parameter_focus")]
            if isinstance(summary.get("broker_truth_focus_parameter_focus"), list)
            else []
        ),
        "broker_truth_focus_primary_experiment": str(summary.get("broker_truth_focus_primary_experiment") or ""),
        "broker_truth_focus_next_command": str(summary.get("broker_truth_focus_next_command") or ""),
        "broker_truth_focus_next_action": str(summary.get("broker_truth_focus_next_action") or ""),
        "broker_truth_focus_active_experiment": (
            dict(summary.get("broker_truth_focus_active_experiment"))
            if isinstance(summary.get("broker_truth_focus_active_experiment"), dict)
            else {}
        ),
        "broker_truth_focus_active_experiment_summary_line": str(
            summary.get("broker_truth_focus_active_experiment_summary_line") or ""
        ),
        "broker_truth_focus_active_experiment_outcome_line": str(
            broker_truth_focus_active_experiment_outcome_line
            or summary.get("broker_truth_focus_active_experiment_outcome_line")
            or snapshot.get("focus_active_experiment_outcome_line")
            or ""
        ),
        "broker_truth_summary_line": str(summary.get("broker_truth_summary_line") or ""),
        "safe_to_mutate_live": bool(summary.get("safe_to_mutate_live") is True),
        "top_bot_id": str(first_bot.get("bot_id") or ""),
        "top_retune_state": str(first_bot.get("retune_state") or ""),
        "top_next_action": str(first_bot.get("next_action") or ""),
        "top_closed_trade_count": int(broker_evidence.get("closed_trade_count") or 0),
        "top_required_closed_trade_count": int(broker_evidence.get("required_closed_trade_count") or 0),
        "top_remaining_closed_trade_count": int(broker_evidence.get("remaining_closed_trade_count") or 0),
        "top_sample_progress_pct": float(broker_evidence.get("sample_progress_pct") or 0.0),
        "top_broker_edge_status": str(broker_evidence.get("edge_status") or ""),
        "top_broker_has_positive_edge": bool(broker_evidence.get("has_positive_edge") is True),
        "top_broker_total_realized_pnl": float(broker_evidence.get("total_realized_pnl") or 0.0),
        "top_broker_profit_factor": float(broker_evidence.get("profit_factor") or 0.0),
        "path": str(path),
        "source": str(snapshot.get("source") or "diamond_retune_status_latest"),
        "updated_at": updated_at,
        "age_s": age_s,
    }
