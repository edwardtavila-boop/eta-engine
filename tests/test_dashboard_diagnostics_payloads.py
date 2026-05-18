from __future__ import annotations

from eta_engine.deploy.scripts.dashboard_diagnostics_payloads import (
    build_dashboard_diagnostics_dirty_worktree_payload,
    build_dashboard_diagnostics_equity_payload,
    build_dashboard_diagnostics_paper_live_payload,
    build_dashboard_diagnostics_readiness_payload,
    build_dashboard_diagnostics_retune_payload,
    build_dashboard_diagnostics_second_brain_payload,
)


def test_build_dashboard_diagnostics_readiness_payload_preserves_launch_truth() -> None:
    payload = build_dashboard_diagnostics_readiness_payload(
        readiness={
            "status": "ready",
            "top_actions": ["sync receipts", "review paper soak"],
            "error": "",
        },
        readiness_summary={
            "can_paper_trade": 10,
            "can_live_any": False,
        },
        readiness_lane_counts={"live_preflight": 6, "paper_soak": 4},
        readiness_blocked_data=3,
    )

    assert payload == {
        "status": "ready",
        "blocked_data": 3,
        "paper_ready": 10,
        "can_live_any": False,
        "launch_lanes": {"live_preflight": 6, "paper_soak": 4},
        "top_action_count": 2,
        "error": "",
    }


def test_build_dashboard_diagnostics_second_brain_payload_uses_rollup_counts() -> None:
    payload = build_dashboard_diagnostics_second_brain_payload(
        second_brain={
            "status": "ready",
            "n_episodes": 44,
            "win_rate": 0.58,
            "avg_r": 1.17,
            "semantic_patterns": 7,
            "procedural_versions": 3,
            "legacy_sources_active": False,
            "sources": {"second_brain": "current"},
            "paths": {"playbook": "var/eta_engine/state/second_brain.json"},
            "error": "",
        },
        eligible_patterns=2,
        favor_pattern_count=1,
        avoid_pattern_count=4,
        truth_note="playbook truth",
    )

    assert payload == {
        "status": "ready",
        "n_episodes": 44,
        "win_rate": 0.58,
        "avg_r": 1.17,
        "semantic_patterns": 7,
        "procedural_versions": 3,
        "eligible_patterns": 2,
        "favor_pattern_count": 1,
        "avoid_pattern_count": 4,
        "legacy_sources_active": False,
        "sources": {"second_brain": "current"},
        "paths": {"playbook": "var/eta_engine/state/second_brain.json"},
        "truth_note": "playbook truth",
        "error": "",
    }


def test_build_dashboard_diagnostics_equity_payload_preserves_equity_contract() -> None:
    payload = build_dashboard_diagnostics_equity_payload(
        equity={
            "source": "supervisor_heartbeat",
            "session_truth_status": "live",
            "source_age_s": 4.5,
            "_error": "",
        },
        equity_series=[{"ts": 1}, {"ts": 2}, {"ts": 3}],
        equity_summary={"today_pnl": 125.75},
    )

    assert payload == {
        "source": "supervisor_heartbeat",
        "session_truth_status": "live",
        "source_age_s": 4.5,
        "point_count": 3,
        "today_pnl": 125.75,
        "error": "",
    }


def test_build_dashboard_diagnostics_equity_payload_coerces_missing_inputs() -> None:
    payload = build_dashboard_diagnostics_equity_payload(
        equity={},
        equity_series="bad",  # type: ignore[arg-type]
        equity_summary="bad",  # type: ignore[arg-type]
    )

    assert payload == {
        "source": "unknown",
        "session_truth_status": "unknown",
        "source_age_s": None,
        "point_count": 0,
        "today_pnl": None,
        "error": None,
    }


def test_build_dashboard_diagnostics_dirty_worktree_payload_sanitizes_lists() -> None:
    payload = build_dashboard_diagnostics_dirty_worktree_payload(
        dirty_worktree_reconciliation={
            "status": "review_required",
            "ready": False,
            "action": "review_child_dirty_groups_before_gitlink_wiring",
            "dirty_modules": ["eta_engine", "mnq_backtest"],
            "blocking_modules": ["eta_engine"],
            "next_actions": ["eta_engine: start with scripts=130"],
            "module_summaries": [{"module": "eta_engine", "entry_count": 444}],
            "review_batches": [{"batch_id": "eta_engine:scripts", "count": 130}],
            "error": "",
        }
    )

    assert payload == {
        "status": "review_required",
        "ready": False,
        "action": "review_child_dirty_groups_before_gitlink_wiring",
        "dirty_modules": ["eta_engine", "mnq_backtest"],
        "blocking_modules": ["eta_engine"],
        "next_actions": ["eta_engine: start with scripts=130"],
        "module_summaries": [{"module": "eta_engine", "entry_count": 444}],
        "review_batches": [{"batch_id": "eta_engine:scripts", "count": 130}],
        "error": "",
    }


def test_build_dashboard_diagnostics_dirty_worktree_payload_coerces_bad_lists_to_empty() -> None:
    payload = build_dashboard_diagnostics_dirty_worktree_payload(
        dirty_worktree_reconciliation={
            "status": "unavailable",
            "ready": True,
            "action": None,
            "dirty_modules": "bad",
            "blocking_modules": None,
            "next_actions": "bad",
            "module_summaries": {"module": "eta_engine"},
            "review_batches": "bad",
            "error": "reconciliation probe exploded",
        }
    )

    assert payload == {
        "status": "unavailable",
        "ready": True,
        "action": "",
        "dirty_modules": [],
        "blocking_modules": [],
        "next_actions": [],
        "module_summaries": [],
        "review_batches": [],
        "error": "reconciliation probe exploded",
    }


def test_build_dashboard_diagnostics_retune_payload_preserves_focus_contract() -> None:
    payload = build_dashboard_diagnostics_retune_payload(
        diamond_retune_status={
            "focus_bot": "mnq_futures_sage",
            "focus_state": "STUCK_RESEARCH_FAILING",
            "focus_issue": "broker_pnl_negative",
            "focus_next_action": "pause repeated attempts",
            "focus_active_experiment": {
                "experiment_id": "partial_profit_disabled",
                "partial_profit_enabled": False,
            },
            "focus_active_experiment_outcome_line": (
                "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
            ),
        },
        diamond_retune_summary={
            "broker_truth_focus_active_experiment_summary_line": (
                "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
            ),
        },
        eta_readiness_snapshot={
            "public_live_retune_generated_at_utc": "2026-05-16T20:33:18+00:00",
            "public_live_retune_focus_active_experiment_outcome_line": (
                "partial_profit_disabled: awaiting first post-change close"
            ),
            "public_live_retune_sync_drift_display": "public retune drift",
            "dashboard_api_runtime_public_live_retune_generated_at_utc": "2026-05-16T20:33:18+00:00",
            "dashboard_api_runtime_public_live_retune_sync_drift_display": "runtime public retune drift",
            "dashboard_api_runtime_retune_drift_display": "8421 retune drift",
            "current_live_retune_generated_at_utc": "2026-05-17T01:25:18+00:00",
            "current_live_retune_focus_active_experiment_outcome_line": (
                "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
            ),
            "current_live_retune_sync_drift_display": "current public retune drift",
            "local_retune_generated_at_utc": "2026-05-16T20:25:28+00:00",
            "local_retune_focus_active_experiment_outcome_line": (
                "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
            ),
            "retune_focus_active_experiment_drift_display": "public vs local retune drift",
            "current_local_retune_generated_at_utc": "2026-05-16T21:25:28+00:00",
            "local_retune_sync_drift_display": "local retune drift",
        },
    )

    assert payload["retune_focus_bot_id"] == "mnq_futures_sage"
    assert payload["retune_focus_state"] == "STUCK_RESEARCH_FAILING"
    assert payload["retune_focus_issue"] == "broker_pnl_negative"
    assert payload["retune_focus_next_action"] == "pause repeated attempts"
    assert payload["retune_focus_active_experiment"] == {
        "experiment_id": "partial_profit_disabled",
        "partial_profit_enabled": False,
    }
    assert (
        payload["retune_focus_active_experiment_summary_line"]
        == "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
    )
    assert (
        payload["retune_focus_active_experiment_outcome_line"]
        == "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
    )
    assert payload["public_live_retune_generated_at_utc"] == "2026-05-16T20:33:18+00:00"
    assert payload["dashboard_api_runtime_retune_drift_display"] == "8421 retune drift"
    assert payload["current_live_retune_generated_at_utc"] == "2026-05-17T01:25:18+00:00"
    assert payload["local_retune_generated_at_utc"] == "2026-05-16T20:25:28+00:00"
    assert payload["current_local_retune_generated_at_utc"] == "2026-05-16T21:25:28+00:00"


def test_build_dashboard_diagnostics_retune_payload_coerces_bad_experiment_to_empty() -> None:
    payload = build_dashboard_diagnostics_retune_payload(
        diamond_retune_status={
            "focus_bot": None,
            "focus_state": None,
            "focus_issue": None,
            "focus_next_action": None,
            "focus_active_experiment": "bad",
            "focus_active_experiment_outcome_line": None,
        },
        diamond_retune_summary={
            "broker_truth_focus_active_experiment_summary_line": None,
        },
        eta_readiness_snapshot={},
    )

    assert payload == {
        "retune_focus_bot_id": "",
        "retune_focus_state": "",
        "retune_focus_issue": "",
        "retune_focus_next_action": "",
        "retune_focus_active_experiment": {},
        "retune_focus_active_experiment_summary_line": "",
        "retune_focus_active_experiment_outcome_line": "",
        "public_live_retune_generated_at_utc": "",
        "public_live_retune_focus_active_experiment_outcome_line": "",
        "public_live_retune_sync_drift_display": "",
        "dashboard_api_runtime_public_live_retune_generated_at_utc": "",
        "dashboard_api_runtime_public_live_retune_sync_drift_display": "",
        "dashboard_api_runtime_retune_drift_display": "",
        "current_live_retune_generated_at_utc": "",
        "current_live_retune_focus_active_experiment_outcome_line": "",
        "current_live_retune_sync_drift_display": "",
        "local_retune_generated_at_utc": "",
        "local_retune_focus_active_experiment_outcome_line": "",
        "retune_focus_active_experiment_drift_display": "",
        "current_local_retune_generated_at_utc": "",
        "local_retune_sync_drift_display": "",
    }


def test_build_dashboard_diagnostics_paper_live_payload_merges_blocked_count_and_age() -> None:
    payload = build_dashboard_diagnostics_paper_live_payload(
        paper_live_transition_summary={
            "status": "ready_to_launch_paper_live",
            "effective_status": "blocked_by_operator_queue",
        },
        operator_summary={"BLOCKED": 3},
        paper_live_transition={"source_age_s": 42.5},
    )

    assert payload == {
        "status": "ready_to_launch_paper_live",
        "effective_status": "blocked_by_operator_queue",
        "operator_queue_blocked_count": 3,
        "source_age_s": 42.5,
    }


def test_build_dashboard_diagnostics_paper_live_payload_coerces_missing_inputs() -> None:
    payload = build_dashboard_diagnostics_paper_live_payload(
        paper_live_transition_summary={},
        operator_summary={"BLOCKED": "bad"},
        paper_live_transition="bad",  # type: ignore[arg-type]
    )

    assert payload == {
        "operator_queue_blocked_count": 0,
        "source_age_s": None,
    }
