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
