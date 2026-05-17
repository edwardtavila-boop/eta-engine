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
