from __future__ import annotations

from eta_engine.deploy.scripts.dashboard_diagnostics_payloads import (
    build_dashboard_diagnostics_diamond_retune_payload,
    build_dashboard_diagnostics_dirty_worktree_payload,
    build_dashboard_diagnostics_equity_payload,
    build_dashboard_diagnostics_paper_live_payload,
    build_dashboard_diagnostics_readiness_payload,
    build_dashboard_diagnostics_retune_payload,
    build_dashboard_diagnostics_second_brain_payload,
    build_dashboard_normalized_diamond_retune_status_payload,
    build_dashboard_retune_focus_overlay_payload,
    build_dashboard_retune_focus_summary_payload,
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


def test_build_dashboard_retune_focus_overlay_payload_preserves_overlay_contract() -> None:
    payload = build_dashboard_retune_focus_overlay_payload(
        snapshot={
            "focus_bot": "mnq_futures_sage",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_issue": "broker_pnl_negative",
            "focus_strategy_kind": "orb_sage_gated",
            "focus_next_action": "Let fresh post-fix closes accumulate.",
            "focus_active_experiment": {"experiment_id": "partial_profit_disabled"},
        },
        readiness_snapshot={
            "public_live_retune_focus_active_experiment_outcome_line": (
                "partial_profit_disabled: awaiting first post-change close"
            ),
            "current_live_retune_generated_at_utc": "2026-05-17T01:25:18+00:00",
            "current_live_retune_focus_active_experiment_outcome_line": (
                "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
            ),
            "current_live_retune_sync_drift_display": "current public retune drift",
            "local_retune_focus_active_experiment_outcome_line": (
                "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
            ),
            "retune_focus_active_experiment_drift_display": "public vs local retune drift",
        },
        focus_active_experiment_summary_line="partial_profit_disabled since 2026-05-16T01:44:06+00:00",
        focus_active_experiment_outcome_line=(
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        ),
    )

    assert payload == {
        "retune_focus_bot_id": "mnq_futures_sage",
        "retune_focus_state": "COLLECT_MORE_SAMPLE",
        "retune_focus_issue": "broker_pnl_negative",
        "retune_focus_strategy_kind": "orb_sage_gated",
        "retune_focus_next_action": "Let fresh post-fix closes accumulate.",
        "retune_focus_active_experiment": {"experiment_id": "partial_profit_disabled"},
        "retune_focus_active_experiment_summary_line": (
            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
        ),
        "retune_focus_active_experiment_outcome_line": (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        ),
        "public_live_retune_focus_active_experiment_outcome_line": (
            "partial_profit_disabled: awaiting first post-change close"
        ),
        "current_live_retune_generated_at_utc": "2026-05-17T01:25:18+00:00",
        "current_live_retune_focus_active_experiment_outcome_line": (
            "partial_profit_disabled: 1 post-change close | R -0.82 | PnL $0.00"
        ),
        "current_live_retune_sync_drift_display": "current public retune drift",
        "local_retune_focus_active_experiment_outcome_line": (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        ),
        "retune_focus_active_experiment_drift_display": "public vs local retune drift",
    }


def test_build_dashboard_retune_focus_overlay_payload_keeps_next_command_fallback() -> None:
    payload = build_dashboard_retune_focus_overlay_payload(
        snapshot={"focus_bot": "mnq_futures_sage", "focus_next_command": "Run broker-proof retune."},
        readiness_snapshot={},
        focus_active_experiment_summary_line="",
        focus_active_experiment_outcome_line="",
    )

    assert payload["retune_focus_next_action"] == "Run broker-proof retune."


def test_build_dashboard_retune_focus_summary_payload_preserves_summary_contract() -> None:
    payload = build_dashboard_retune_focus_summary_payload(
        snapshot={
            "focus_bot": "mnq_futures_sage",
            "focus_state": "COLLECT_MORE_SAMPLE",
            "focus_issue": "broker_pnl_negative",
            "focus_next_action": "Let fresh post-fix closes accumulate.",
            "focus_active_experiment": {"experiment_id": "partial_profit_disabled"},
        },
        readiness_snapshot={
            "public_live_retune_focus_active_experiment_outcome_line": (
                "partial_profit_disabled: awaiting first post-change close"
            ),
            "local_retune_focus_active_experiment_outcome_line": (
                "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
            ),
            "retune_focus_active_experiment_drift_display": "public vs local retune drift",
        },
        focus_active_experiment_summary_line="partial_profit_disabled since 2026-05-16T01:44:06+00:00",
        focus_active_experiment_outcome_line=(
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        ),
    )

    assert payload == {
        "retune_focus_bot_id": "mnq_futures_sage",
        "retune_focus_state": "COLLECT_MORE_SAMPLE",
        "retune_focus_issue": "broker_pnl_negative",
        "retune_focus_next_action": "Let fresh post-fix closes accumulate.",
        "retune_focus_active_experiment": {"experiment_id": "partial_profit_disabled"},
        "retune_focus_active_experiment_summary_line": (
            "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
        ),
        "retune_focus_active_experiment_outcome_line": (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        ),
        "public_live_retune_focus_active_experiment_outcome_line": (
            "partial_profit_disabled: awaiting first post-change close"
        ),
        "local_retune_focus_active_experiment_outcome_line": (
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        ),
        "retune_focus_active_experiment_drift_display": "public vs local retune drift",
    }


def test_build_dashboard_normalized_diamond_retune_status_payload_preserves_alias_contract() -> None:
    payload = build_dashboard_normalized_diamond_retune_status_payload(
        payload={
            "kind": "eta_diamond_retune_status",
            "summary": {
                "n_targets": 5,
                "n_attempted_bots": 2,
                "n_unattempted_targets": 3,
                "n_low_sample_keep_collecting": 1,
                "n_near_miss_keep_tuning": 1,
                "n_unstable_positive_keep_tuning": 1,
                "n_stuck_research_failing": 1,
                "n_research_passed_broker_proof_required": 1,
                "broker_truth_focus_issue_code": "broker_pnl_negative",
                "broker_truth_focus_strategy_kind": "orb_sage_gated",
                "broker_truth_focus_worst_session": "overnight",
                "broker_truth_focus_parameter_focus": ["session predicate", "rr_target"],
                "broker_truth_focus_next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mnq_futures_sage --report-policy runtime"
                ),
                "broker_truth_focus_active_experiment": {
                    "experiment_id": "partial_profit_disabled",
                    "partial_profit_enabled": False,
                },
                "broker_truth_focus_active_experiment_summary_line": (
                    "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
                ),
                "safe_to_mutate_live": False,
            },
            "bots": [
                {"bot_id": "mnq_futures_sage", "stage": "stuck_research_failing"},
                {"bot_id": "mcl_sweep_reclaim", "stage": "research_passed_broker_proof_required"},
            ],
        },
        path="C:/EvolutionaryTradingAlgo/var/eta_engine/state/diamond_retune_status_latest.json",
        focus_active_experiment_outcome_line=(
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        ),
    )

    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["contract_ok"] is True
    assert payload["summary"]["n_targets"] == 5
    assert payload["summary"]["n_low_sample_keep_collecting"] == 1
    assert payload["summary"]["broker_truth_focus_issue_code"] == "broker_pnl_negative"
    assert payload["summary"]["broker_truth_focus_parameter_focus"] == ["session predicate", "rr_target"]
    assert payload["summary"]["broker_truth_focus_active_experiment"]["experiment_id"] == "partial_profit_disabled"
    assert (
        payload["summary"]["broker_truth_focus_active_experiment_outcome_line"]
        == "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
    )
    assert payload["focus_bot"] == "mnq_futures_sage"
    assert payload["focus_issue"] == "broker_pnl_negative"
    assert payload["focus_state"] == "stuck_research_failing"
    assert payload["focus_strategy_kind"] == "orb_sage_gated"
    assert payload["focus_worst_session"] == "overnight"
    assert payload["focus_parameter_focus"] == ["session predicate", "rr_target"]
    assert payload["focus_command"].endswith("--bots mnq_futures_sage --report-policy runtime")
    assert payload["focus_active_experiment"]["partial_profit_enabled"] is False
    assert (
        payload["focus_active_experiment_outcome_line"]
        == "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
    )
    assert payload["source_path"].endswith("diamond_retune_status_latest.json")
    assert payload["safe_to_mutate_live"] is False


def test_build_dashboard_normalized_diamond_retune_status_payload_fails_closed_on_bad_contract_shape() -> None:
    payload = build_dashboard_normalized_diamond_retune_status_payload(
        payload={
            "kind": "eta_diamond_retune_status",
            "summary": "bad",
            "bots": "bad",
            "research_backlog": "bad",
        },
        path="diamond_retune_status_latest.json",
        focus_active_experiment_outcome_line="",
    )

    assert payload["status"] == "invalid"
    assert payload["ready"] is False
    assert payload["contract_ok"] is False
    assert payload["summary"]["n_targets"] == 0
    assert payload["summary"]["n_research_backlog_targets"] == 0
    assert payload["bots"] == []
    assert payload["research_backlog"] == []
    assert payload["focus_bot"] == ""
    assert payload["focus_active_experiment"] == {}


def test_build_dashboard_diagnostics_diamond_retune_payload_preserves_summary_and_top_bot_contract() -> None:
    payload = build_dashboard_diagnostics_diamond_retune_payload(
        snapshot={
            "status": "ready",
            "ready": True,
            "contract_ok": True,
            "source": "diamond_retune_status_latest",
            "focus_active_experiment_outcome_line": (
                "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
            ),
            "summary": {
                "n_targets": 3,
                "n_attempted_bots": 2,
                "n_unattempted_targets": 1,
                "n_stuck_research_failing": 1,
                "n_broker_proof_shortfall": 1,
                "largest_broker_proof_gap": 91,
                "total_broker_proof_gap": 91,
                "broker_truth_focus_bot_id": "mnq_futures_sage",
                "broker_truth_focus_edge_status": "sample_met_negative_edge",
                "broker_truth_focus_closed_trade_count": 126,
                "broker_truth_focus_remaining_closed_trade_count": 0,
                "broker_truth_focus_total_realized_pnl": -1939.75,
                "broker_truth_focus_profit_factor": 0.3951,
                "broker_truth_focus_issue_code": "broker_pnl_negative",
                "broker_truth_focus_priority_score": 1061.81,
                "broker_truth_focus_strategy_kind": "orb_sage_gated",
                "broker_truth_focus_best_session": "close",
                "broker_truth_focus_worst_session": "overnight",
                "broker_truth_focus_parameter_focus": ["session predicate", "rr_target"],
                "broker_truth_focus_primary_experiment": "Bias fresh sample toward close and block overnight.",
                "broker_truth_focus_next_command": (
                    "python -m eta_engine.scripts.run_research_grid --bots "
                    "mnq_futures_sage"
                ),
                "broker_truth_focus_next_action": "pause repeated attempts",
                "broker_truth_focus_active_experiment": {
                    "experiment_id": "partial_profit_disabled",
                    "partial_profit_enabled": False,
                },
                "broker_truth_focus_active_experiment_summary_line": (
                    "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
                ),
                "broker_truth_summary_line": "mnq_futures_sage: sample met (126/100) but broker edge is negative",
                "safe_to_mutate_live": False,
            },
            "bots": [
                {
                    "bot_id": "mnq_futures_sage",
                    "retune_state": "STUCK_RESEARCH_FAILING",
                    "next_action": "pause repeated attempts",
                    "broker_close_evidence": {
                        "closed_trade_count": 9,
                        "required_closed_trade_count": 100,
                        "remaining_closed_trade_count": 91,
                        "sample_progress_pct": 9.0,
                        "edge_status": "needs_more_broker_closes",
                        "has_positive_edge": False,
                        "total_realized_pnl": -125.25,
                        "profit_factor": 0.72,
                    },
                }
            ],
        },
        path="C:/EvolutionaryTradingAlgo/var/eta_engine/state/diamond_retune_status_latest.json",
        updated_at="2026-05-17T23:59:00+00:00",
        age_s=17,
        broker_truth_focus_active_experiment_outcome_line=(
            "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
        ),
    )

    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["contract_ok"] is True
    assert payload["n_targets"] == 3
    assert payload["n_attempted_bots"] == 2
    assert payload["n_broker_proof_shortfall"] == 1
    assert payload["broker_truth_focus_bot_id"] == "mnq_futures_sage"
    assert payload["broker_truth_focus_active_experiment"]["experiment_id"] == "partial_profit_disabled"
    assert (
        payload["broker_truth_focus_active_experiment_summary_line"]
        == "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
    )
    assert (
        payload["broker_truth_focus_active_experiment_outcome_line"]
        == "partial_profit_disabled: 2 post-change closes | R +0.82 | PnL $40.00 | PF 1.50"
    )
    assert payload["top_bot_id"] == "mnq_futures_sage"
    assert payload["top_remaining_closed_trade_count"] == 91
    assert payload["top_sample_progress_pct"] == 9.0
    assert payload["top_broker_profit_factor"] == 0.72
    assert payload["path"] == "C:/EvolutionaryTradingAlgo/var/eta_engine/state/diamond_retune_status_latest.json"
    assert payload["updated_at"] == "2026-05-17T23:59:00+00:00"
    assert payload["age_s"] == 17


def test_build_dashboard_diagnostics_diamond_retune_payload_coerces_missing_lists_to_defaults() -> None:
    payload = build_dashboard_diagnostics_diamond_retune_payload(
        snapshot={
            "status": None,
            "ready": False,
            "contract_ok": False,
            "summary": "bad",
            "bots": "bad",
        },
        path="diamond_retune_status_latest.json",
        updated_at=None,
        age_s=None,
        broker_truth_focus_active_experiment_outcome_line="",
    )

    assert payload == {
        "status": "missing",
        "ready": False,
        "contract_ok": False,
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
        "top_bot_id": "",
        "top_retune_state": "",
        "top_next_action": "",
        "top_closed_trade_count": 0,
        "top_required_closed_trade_count": 0,
        "top_remaining_closed_trade_count": 0,
        "top_sample_progress_pct": 0.0,
        "top_broker_edge_status": "",
        "top_broker_has_positive_edge": False,
        "top_broker_total_realized_pnl": 0.0,
        "top_broker_profit_factor": 0.0,
        "path": "diamond_retune_status_latest.json",
        "source": "diamond_retune_status_latest",
        "updated_at": None,
        "age_s": None,
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
