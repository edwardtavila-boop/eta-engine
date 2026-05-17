"""Tests for the diamond retune status summary."""

from __future__ import annotations


def test_status_summarizes_attempts_and_next_actions() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-14T20:00:00+00:00",
        "summary": {"n_selected_targets": 3, "safe_to_mutate_live": False},
        "targets": [
            {
                "rank": 1,
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "asset_sleeve": "equity_index",
                "strategy_kind": "orb_sage_gated",
                "issue_code": "broker_pnl_negative",
                "priority_score": 1000.0,
                "best_session": "close",
                "worst_session": "overnight",
                "parameter_focus": ["session predicate", "rr_target"],
                "primary_experiment": "Bias fresh sample toward close and block overnight.",
                "next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mnq_futures_sage --report-policy runtime"
                ),
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
            {
                "rank": 2,
                "bot_id": "nq_futures_sage",
                "symbol": "NQ1",
                "asset_sleeve": "equity_index",
                "priority_score": 900.0,
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
            {
                "rank": 3,
                "bot_id": "mcl_sweep_reclaim",
                "symbol": "MCL1",
                "asset_sleeve": "metals_energy",
                "priority_score": 300.0,
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
    }
    history_rows = [
        {
            "run_id": "a",
            "generated_at_utc": "2026-05-14T20:01:00+00:00",
            "bot_id": "mnq_futures_sage",
            "rank": 1,
            "status": "research_failed_keep_retuning",
            "exit_code": 1,
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
        {
            "run_id": "b",
            "generated_at_utc": "2026-05-14T21:01:00+00:00",
            "bot_id": "mnq_futures_sage",
            "rank": 1,
            "status": "research_failed_keep_retuning",
            "exit_code": 1,
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
        {
            "run_id": "c",
            "generated_at_utc": "2026-05-14T22:01:00+00:00",
            "bot_id": "mnq_futures_sage",
            "rank": 1,
            "status": "research_failed_keep_retuning",
            "exit_code": 1,
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
        {
            "run_id": "d",
            "generated_at_utc": "2026-05-14T23:01:00+00:00",
            "bot_id": "nq_futures_sage",
            "rank": 2,
            "status": "research_low_sample_keep_collecting",
            "exit_code": 1,
            "research_signal": {
                "classification": "LOW_SAMPLE_PROMISING",
                "windows": 1,
                "agg_oos": 6.803,
                "pass_frac_pct": 100.0,
            },
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
    ]

    closed_trade_ledger = {
        "generated_at_utc": "2026-05-14T23:30:00+00:00",
        "data_sources_filter": ["live", "paper"],
        "per_bot": {
            "nq_futures_sage": {
                "closed_trade_count": 36,
                "total_realized_pnl": -782.32,
                "cumulative_r": -1.25,
                "profit_factor": 0.92,
                "win_rate_pct": 48.0,
            }
        },
    }

    report = status.build_status(
        campaign=campaign,
        history_rows=history_rows,
        closed_trade_ledger=closed_trade_ledger,
    )

    assert report["kind"] == "eta_diamond_retune_status"
    assert report["summary"]["n_targets"] == 3
    assert report["summary"]["n_attempted_bots"] == 2
    assert report["summary"]["n_unattempted_targets"] == 1
    assert report["summary"]["n_research_passed_broker_proof_required"] == 0
    assert report["summary"]["n_low_sample_keep_collecting"] == 1
    assert report["summary"]["broker_proof_required_closes"] == 100
    assert report["summary"]["n_broker_sample_ready"] == 0
    assert report["summary"]["n_broker_edge_ready"] == 0
    assert report["summary"]["n_broker_proof_ready"] == 0
    assert report["summary"]["n_broker_sample_ready_negative_edge"] == 0
    assert report["summary"]["n_broker_proof_shortfall"] == 3
    assert report["summary"]["largest_broker_proof_gap"] == 100
    assert report["summary"]["total_broker_proof_gap"] == 264
    assert report["summary"]["safe_to_mutate_live"] is False
    assert report["summary"]["broker_truth_focus_issue_code"] == "broker_pnl_negative"
    assert report["summary"]["broker_truth_focus_strategy_kind"] == "orb_sage_gated"
    assert report["summary"]["broker_truth_focus_best_session"] == "close"
    assert report["summary"]["broker_truth_focus_worst_session"] == "overnight"
    assert report["summary"]["broker_truth_focus_parameter_focus"] == ["session predicate", "rr_target"]
    assert report["summary"]["broker_truth_focus_primary_experiment"] == (
        "Bias fresh sample toward close and block overnight."
    )
    assert report["summary"]["broker_truth_focus_next_command"].endswith(
        "--bots mnq_futures_sage --report-policy runtime"
    )
    assert report["bots"][0]["bot_id"] == "mnq_futures_sage"
    assert report["bots"][0]["retune_state"] == "STUCK_RESEARCH_FAILING"
    assert report["bots"][0]["issue_code"] == "broker_pnl_negative"
    assert report["bots"][0]["strategy_kind"] == "orb_sage_gated"
    assert report["bots"][0]["best_session"] == "close"
    assert report["bots"][0]["worst_session"] == "overnight"
    assert report["bots"][0]["parameter_focus"] == ["session predicate", "rr_target"]
    assert report["bots"][0]["primary_experiment"] == "Bias fresh sample toward close and block overnight."
    assert report["bots"][0]["next_command"].endswith(
        "--bots mnq_futures_sage --report-policy runtime"
    )
    assert report["bots"][0]["attempts"] == 3
    assert "new hypothesis" in report["bots"][0]["next_action"]
    assert report["bots"][1]["bot_id"] == "nq_futures_sage"
    assert report["bots"][1]["retune_state"] == "COLLECT_MORE_SAMPLE"
    assert "collect 64 more paper/broker closes (36/100)" in report["bots"][1]["next_action"]
    assert report["bots"][1]["research_signal"]["classification"] == "LOW_SAMPLE_PROMISING"
    assert report["bots"][1]["broker_close_evidence"]["closed_trade_count"] == 36
    assert report["bots"][1]["broker_close_evidence"]["remaining_closed_trade_count"] == 64
    assert report["bots"][1]["broker_close_evidence"]["sample_progress_pct"] == 36.0
    assert report["bots"][1]["safe_to_mutate_live"] is False
    assert report["bots"][2]["bot_id"] == "mcl_sweep_reclaim"
    assert report["bots"][2]["retune_state"] == "NOT_ATTEMPTED"


def test_status_surfaces_research_backlog_without_mixing_broker_targets() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-14T20:00:00+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "asset_sleeve": "equity_index",
                "strategy_kind": "orb_sage_gated",
                "issue_code": "broker_pnl_negative",
                "priority_score": 1000.0,
                "best_session": "close",
                "worst_session": "overnight",
                "parameter_focus": ["session predicate", "opening range boundary", "sage_min_alignment"],
                "primary_experiment": "Concentrate paper sample around close and block overnight entries.",
                "next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mnq_futures_sage --report-policy runtime"
                ),
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
        "research_backlog": [
            {
                "rank": 1,
                "bot_id": "mes_sweep_reclaim_v2",
                "strategy_id": "mes_sweep_reclaim_v2",
                "issue_code": "research_gate_failed",
                "summary": "research_candidate (strict gate failed; OOS +0.499)",
                "research_signal": {
                    "agg_oos_sharpe": 0.499,
                    "dsr_pass_fraction": 0.273,
                    "strict_gate": False,
                    "windows": 11,
                },
                "next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mes_sweep_reclaim_v2 --report-policy runtime"
                ),
                "verification_command": (
                    "python -m eta_engine.scripts.paper_live_launch_check "
                    "--bots mes_sweep_reclaim_v2 --json"
                ),
                "promotion_block": "research_gate_required",
                "live_mutation_policy": "paper_only_advisory",
                "safe_to_mutate_live": False,
            },
        ],
    }

    report = status.build_status(campaign=campaign, history_rows=[])

    assert report["summary"]["n_targets"] == 1
    assert report["summary"]["n_research_backlog_targets"] == 1
    assert report["bots"][0]["bot_id"] == "mnq_futures_sage"
    assert report["research_backlog"][0]["bot_id"] == "mes_sweep_reclaim_v2"
    assert report["research_backlog"][0]["promotion_block"] == "research_gate_required"
    assert report["research_backlog"][0]["retune_state"] == "RESEARCH_GATE_FAILED"
    assert report["research_backlog"][0]["next_action"] == (
        "rerun runtime-only research grid, then launch-check; no live changes"
    )
    assert report["research_backlog"][0]["safe_to_mutate_live"] is False


def test_status_surfaces_focus_active_experiment_from_retune_advisory() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-16T01:44:06+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "mnq_futures_sage",
                "strategy_kind": "orb_sage_gated",
                "issue_code": "broker_pnl_negative",
                "primary_experiment": "Disable partial profit and resoak.",
                "next_command": "python -m eta_engine.scripts.run_research_grid --bots mnq_futures_sage",
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            }
        ],
    }

    report = status.build_status(
        campaign=campaign,
        history_rows=[],
        retune_advisory={
            "focus_bot": "mnq_futures_sage",
            "preferred_action": (
                "Await the first post-fix close for mnq_futures_sage; latest broker-proof close for this bot was "
                "2026-05-15T20:59:35.998873+00:00, before experiment start 2026-05-16T01:44:06+00:00."
            ),
            "active_experiment": {
                "experiment_id": "partial_profit_disabled",
                "started_at": "2026-05-16T01:44:06+00:00",
                "partial_profit_enabled": False,
                "post_change_closed_trade_count": 2,
                "post_change_total_realized_pnl": 40.0,
                "post_change_profit_factor": 1.5,
            },
        },
    )

    assert report["summary"]["broker_truth_focus_active_experiment"]["experiment_id"] == "partial_profit_disabled"
    assert report["summary"]["broker_truth_focus_active_experiment"]["partial_profit_enabled"] is False
    assert report["summary"]["broker_truth_focus_active_experiment_summary_line"] == (
        "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
    )
    assert report["focus_next_action"] == (
        "Await the first post-fix close for mnq_futures_sage; latest broker-proof close for this bot was "
        "2026-05-15T20:59:35.998873+00:00, before experiment start 2026-05-16T01:44:06+00:00."
    )
    assert report["focus_active_experiment"]["post_change_closed_trade_count"] == 2
    assert report["focus_active_experiment_summary_line"] == (
        "partial_profit_disabled since 2026-05-16T01:44:06+00:00"
    )


def test_status_prefers_public_retune_truth_for_operator_focus_summary() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-15T20:00:00+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "mcl_sweep_reclaim",
                "symbol": "MCL1",
                "asset_sleeve": "energy",
                "priority_score": 50.0,
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
    }
    local_ledger = {
        "generated_at_utc": "2026-05-15T20:05:00+00:00",
        "per_bot": {
            "mcl_sweep_reclaim": {
                "closed_trade_count": 5,
                "total_realized_pnl": -151.0,
                "profit_factor": 0.0,
                "win_rate_pct": 0.0,
            },
        },
    }
    public_retune_truth = {
        "generated_at_utc": "2026-05-15T20:10:00+00:00",
        "surface": {
            "observed_ts": "2026-05-15T20:09:30+00:00",
            "normalized": {
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_strategy_kind": "orb_sage_gated",
                "focus_best_session": "close",
                "focus_worst_session": "overnight",
                "focus_command": "python -m eta_engine.scripts.run_research_grid --bots mnq_futures_sage",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
                "safe_to_mutate_live": False,
            },
            "summary": {
                "broker_truth_summary_line": (
                    "mnq_futures_sage: sample met (141/100) but broker edge is negative; retune or demote."
                ),
            },
        },
    }

    report = status.build_status(
        campaign=campaign,
        history_rows=[],
        closed_trade_ledger=local_ledger,
        public_retune_truth=public_retune_truth,
    )

    assert report["focus_bot"] == "mnq_futures_sage"
    assert report["focus_issue"] == "broker_pnl_negative"
    assert report["focus_state"] == "COLLECT_MORE_SAMPLE"
    assert report["focus_strategy_kind"] == "orb_sage_gated"
    assert report["focus_command"] == "python -m eta_engine.scripts.run_research_grid --bots mnq_futures_sage"
    assert report["focus_closed_trade_count"] == 141
    assert report["summary"]["public_truth_override_applied"] is True
    assert report["summary"]["broker_truth_focus_source"] == "public_diamond_retune_truth_cache"
    assert report["summary"]["local_broker_truth_focus_bot_id"] == "mcl_sweep_reclaim"
    assert report["summary"]["broker_truth_focus_edge_status"] == "sample_met_negative_edge"
    assert report["summary"]["broker_truth_focus_remaining_closed_trade_count"] == 0
    assert "sample met (141/100) but broker edge is negative" in report["summary"]["broker_truth_focus_next_action"]
    assert "broker edge is negative" in report["summary"]["broker_truth_summary_line"]


def test_status_prefers_public_broker_close_cache_when_local_sample_is_thin() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-15T20:00:00+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "asset_sleeve": "equity_index",
                "strategy_kind": "orb_sage_gated",
                "issue_code": "broker_pnl_negative",
                "priority_score": 1000.0,
                "best_session": "close",
                "worst_session": "overnight",
                "parameter_focus": ["session predicate", "rr_target"],
                "primary_experiment": "Bias fresh sample toward close and block overnight.",
                "next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mnq_futures_sage --report-policy runtime"
                ),
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
    }
    local_ledger = {
        "generated_at_utc": "2026-05-15T20:05:00+00:00",
        "data_sources_filter": ["live", "paper"],
        "per_bot": {
            "mnq_futures_sage": {
                "closed_trade_count": 5,
                "total_realized_pnl": -151.0,
                "profit_factor": 0.0,
                "win_rate_pct": 0.0,
            },
        },
    }
    public_broker_close_truth_cache = {
        "generated_at_utc": "2026-05-15T20:10:00+00:00",
        "surface": {
            "normalized": {
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
                "broker_snapshot_source": "ibkr_probe_cache",
                "reporting_timezone": "America/New_York",
            },
        },
    }

    report = status.build_status(
        campaign=campaign,
        history_rows=[],
        closed_trade_ledger=local_ledger,
        public_broker_close_truth_cache=public_broker_close_truth_cache,
    )

    assert report["focus_bot"] == "mnq_futures_sage"
    assert report["focus_closed_trade_count"] == 141
    assert report["summary"]["broker_truth_focus_source"] == "public_broker_close_truth_cache"
    assert report["summary"]["broker_truth_focus_advisory_override_applied"] is True
    assert report["bots"][0]["broker_close_evidence"]["source"] == "public_broker_close_truth_cache"
    assert report["bots"][0]["broker_close_evidence"]["advisory_override_applied"] is True
    assert report["bots"][0]["broker_close_evidence"]["advisory_override_reason"] == "public_sample_stronger_than_local"
    assert report["bots"][0]["broker_close_evidence"]["local_closed_trade_count"] == 5
    assert report["bots"][0]["broker_close_evidence"]["closed_trade_count"] == 141
    assert report["bots"][0]["broker_close_evidence"]["edge_status"] == "sample_met_negative_edge"
    assert "sample met (141/100) but broker edge is negative" in report["summary"]["broker_truth_summary_line"]


def test_status_falls_back_to_retune_truth_check_when_public_retune_cache_is_skinny() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-15T20:00:00+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "mcl_sweep_reclaim",
                "symbol": "MCL1",
                "asset_sleeve": "energy",
                "priority_score": 50.0,
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
    }
    local_ledger = {
        "generated_at_utc": "2026-05-15T20:05:00+00:00",
        "per_bot": {
            "mcl_sweep_reclaim": {
                "closed_trade_count": 5,
                "total_realized_pnl": -151.0,
                "profit_factor": 0.0,
            },
        },
    }
    skinny_public_retune_truth = {
        "generated_at_utc": "2026-05-15T20:10:00+00:00",
        "surface": {
            "available": True,
            "readable": True,
        },
        "focus_bot": None,
        "focus_issue": None,
        "focus_state": None,
    }
    public_retune_truth_check = {
        "public_surface": {
            "available": True,
            "readable": True,
            "normalized": {
                "focus_bot": "mnq_futures_sage",
                "focus_issue": "broker_pnl_negative",
                "focus_state": "COLLECT_MORE_SAMPLE",
                "focus_strategy_kind": "orb_sage_gated",
                "focus_best_session": "close",
                "focus_worst_session": "overnight",
                "focus_command": "python -m eta_engine.scripts.run_research_grid --bots mnq_futures_sage",
                "focus_closed_trade_count": 141,
                "focus_total_realized_pnl": -1939.75,
                "focus_profit_factor": 0.3951,
                "safe_to_mutate_live": False,
            },
            "summary": {
                "broker_truth_summary_line": (
                    "mnq_futures_sage: sample met (141/100) but broker edge is negative; retune or demote."
                ),
            },
        },
    }

    report = status.build_status(
        campaign=campaign,
        history_rows=[],
        closed_trade_ledger=local_ledger,
        public_retune_truth=skinny_public_retune_truth,
        public_retune_truth_check=public_retune_truth_check,
    )

    assert report["focus_bot"] == "mnq_futures_sage"
    assert report["focus_issue"] == "broker_pnl_negative"
    assert report["focus_closed_trade_count"] == 141
    assert report["summary"]["public_truth_override_applied"] is True
    assert report["summary"]["broker_truth_focus_source"] == "diamond_retune_truth_check_public_surface"
    assert report["summary"]["local_broker_truth_focus_bot_id"] == "mcl_sweep_reclaim"


def test_status_surfaces_near_miss_tuning_without_live_mutation() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-14T20:00:00+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "met_sweep_reclaim",
                "symbol": "MET1",
                "asset_sleeve": "metals_energy",
                "priority_score": 88.4,
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
    }
    history_rows = [
        {
            "run_id": "near-miss",
            "generated_at_utc": "2026-05-14T23:01:00+00:00",
            "bot_id": "met_sweep_reclaim",
            "rank": 1,
            "status": "research_near_miss_keep_tuning",
            "exit_code": 1,
            "research_signal": {
                "classification": "NEAR_MISS_TUNE",
                "windows": 6,
                "agg_oos": 0.044,
                "pass_frac_pct": 66.7,
            },
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
    ]

    report = status.build_status(campaign=campaign, history_rows=history_rows)

    assert report["summary"]["n_near_miss_keep_tuning"] == 1
    assert report["summary"]["safe_to_mutate_live"] is False
    assert report["bots"][0]["retune_state"] == "NEAR_MISS_RETUNE"
    assert "focused tuning" in report["bots"][0]["next_action"]
    assert "no live changes" in report["bots"][0]["next_action"]
    assert report["bots"][0]["research_signal"]["classification"] == "NEAR_MISS_TUNE"
    assert report["bots"][0]["safe_to_mutate_live"] is False


def test_status_surfaces_unstable_positive_tuning_without_live_mutation() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-14T20:00:00+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "ng_sweep_reclaim",
                "symbol": "NG1",
                "asset_sleeve": "metals_energy",
                "priority_score": 78.2,
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
    }
    history_rows = [
        {
            "run_id": "unstable",
            "generated_at_utc": "2026-05-14T23:12:00+00:00",
            "bot_id": "ng_sweep_reclaim",
            "rank": 1,
            "status": "research_unstable_positive_keep_tuning",
            "exit_code": 1,
            "research_signal": {
                "classification": "UNSTABLE_POSITIVE_TUNE",
                "windows": 12,
                "agg_oos": 0.734,
                "pass_frac_pct": 41.7,
            },
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
    ]

    report = status.build_status(campaign=campaign, history_rows=history_rows)

    assert report["summary"]["n_unstable_positive_keep_tuning"] == 1
    assert report["summary"]["safe_to_mutate_live"] is False
    assert report["bots"][0]["retune_state"] == "UNSTABLE_POSITIVE_RETUNE"
    assert "consistency" in report["bots"][0]["next_action"]
    assert "no live changes" in report["bots"][0]["next_action"]
    assert report["bots"][0]["research_signal"]["classification"] == "UNSTABLE_POSITIVE_TUNE"
    assert report["bots"][0]["safe_to_mutate_live"] is False


def test_status_distinguishes_research_window_gap_from_broker_close_gap() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-14T20:00:00+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "asset_sleeve": "equity_index",
                "strategy_kind": "orb_sage_gated",
                "issue_code": "broker_pnl_negative",
                "priority_score": 1000.0,
                "best_session": "close",
                "worst_session": "overnight",
                "parameter_focus": ["session predicate", "opening range boundary", "sage_min_alignment"],
                "primary_experiment": "Concentrate paper sample around close and block overnight entries.",
                "next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mnq_futures_sage --report-policy runtime"
                ),
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
    }
    history_rows = [
        {
            "run_id": "low-sample",
            "generated_at_utc": "2026-05-14T23:01:00+00:00",
            "bot_id": "mnq_futures_sage",
            "rank": 1,
            "status": "research_low_sample_keep_collecting",
            "exit_code": 1,
            "research_signal": {
                "classification": "LOW_SAMPLE_PROMISING",
                "windows": 1,
                "agg_oos": 6.803,
                "pass_frac_pct": 100.0,
            },
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
    ]
    closed_trade_ledger = {
        "generated_at_utc": "2026-05-14T23:30:00+00:00",
        "data_sources_filter": ["live", "paper"],
        "per_bot": {"mnq_futures_sage": {"closed_trade_count": 126}},
    }

    report = status.build_status(
        campaign=campaign,
        history_rows=history_rows,
        closed_trade_ledger=closed_trade_ledger,
    )

    assert report["summary"]["n_broker_sample_ready"] == 1
    assert report["summary"]["n_broker_edge_ready"] == 0
    assert report["summary"]["n_broker_proof_ready"] == 0
    assert report["summary"]["n_broker_sample_ready_negative_edge"] == 1
    assert report["summary"]["n_broker_proof_shortfall"] == 0
    assert report["summary"]["broker_truth_focus_bot_id"] == "mnq_futures_sage"
    assert report["summary"]["broker_truth_focus_edge_status"] == "sample_met_negative_edge"
    assert report["summary"]["broker_truth_focus_closed_trade_count"] == 126
    assert report["summary"]["broker_truth_focus_remaining_closed_trade_count"] == 0
    assert report["summary"]["broker_truth_focus_total_realized_pnl"] == 0.0
    assert report["summary"]["broker_truth_focus_issue_code"] == "broker_pnl_negative"
    assert report["summary"]["broker_truth_focus_strategy_kind"] == "orb_sage_gated"
    assert report["summary"]["broker_truth_focus_best_session"] == "close"
    assert report["summary"]["broker_truth_focus_worst_session"] == "overnight"
    assert report["summary"]["broker_truth_focus_parameter_focus"] == [
        "session predicate",
        "opening range boundary",
        "sage_min_alignment",
    ]
    assert report["summary"]["broker_truth_focus_primary_experiment"] == (
        "Concentrate paper sample around close and block overnight entries."
    )
    assert report["summary"]["broker_truth_focus_next_command"].endswith(
        "--bots mnq_futures_sage --report-policy runtime"
    )
    assert "mnq_futures_sage: sample met (126/100) but broker edge is negative" in report["summary"][
        "broker_truth_summary_line"
    ]
    assert report["bots"][0]["broker_close_evidence"]["remaining_closed_trade_count"] == 0
    assert report["bots"][0]["broker_close_evidence"]["edge_status"] == "sample_met_negative_edge"
    assert report["bots"][0]["broker_close_evidence"]["has_positive_edge"] is False
    assert report["bots"][0]["issue_code"] == "broker_pnl_negative"
    assert report["bots"][0]["parameter_focus"] == [
        "session predicate",
        "opening range boundary",
        "sage_min_alignment",
    ]
    assert report["bots"][0]["next_command"].endswith(
        "--bots mnq_futures_sage --report-policy runtime"
    )
    assert "sample met (126/100) but broker edge is negative" in report["bots"][0]["next_action"]
    assert "retune or demote" in report["bots"][0]["next_action"]


def test_status_counts_broker_proof_ready_only_when_sample_is_profitable() -> None:
    from eta_engine.scripts import diamond_retune_status as status

    campaign = {
        "generated_at_utc": "2026-05-14T20:00:00+00:00",
        "targets": [
            {
                "rank": 1,
                "bot_id": "mbt_funding_basis",
                "symbol": "MBT",
                "asset_sleeve": "crypto",
                "priority_score": 1000.0,
                "promotion_block": "broker_proof_required",
                "safe_to_mutate_live": False,
            },
        ],
    }
    history_rows = [
        {
            "run_id": "positive-sample",
            "generated_at_utc": "2026-05-14T23:01:00+00:00",
            "bot_id": "mbt_funding_basis",
            "rank": 1,
            "status": "research_low_sample_keep_collecting",
            "exit_code": 1,
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
    ]
    closed_trade_ledger = {
        "generated_at_utc": "2026-05-14T23:30:00+00:00",
        "data_sources_filter": ["live", "paper"],
        "per_bot": {
            "mbt_funding_basis": {
                "closed_trade_count": 126,
                "total_realized_pnl": 275.50,
                "profit_factor": 1.42,
            }
        },
    }

    report = status.build_status(
        campaign=campaign,
        history_rows=history_rows,
        closed_trade_ledger=closed_trade_ledger,
    )

    assert report["summary"]["n_broker_sample_ready"] == 1
    assert report["summary"]["n_broker_edge_ready"] == 1
    assert report["summary"]["n_broker_proof_ready"] == 1
    assert report["summary"]["n_broker_sample_ready_negative_edge"] == 0
    assert report["summary"]["broker_truth_focus_bot_id"] == "mbt_funding_basis"
    assert report["summary"]["broker_truth_focus_edge_status"] == "broker_edge_ready"
    assert report["summary"]["broker_truth_focus_total_realized_pnl"] == 275.5
    assert "broker sample is positive" in report["summary"]["broker_truth_summary_line"]
    assert report["bots"][0]["broker_close_evidence"]["edge_status"] == "broker_edge_ready"
    assert report["bots"][0]["broker_close_evidence"]["has_positive_edge"] is True
