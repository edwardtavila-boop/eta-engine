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
                "priority_score": 1000.0,
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
    assert report["summary"]["n_broker_proof_ready"] == 0
    assert report["summary"]["n_broker_proof_shortfall"] == 3
    assert report["summary"]["largest_broker_proof_gap"] == 100
    assert report["summary"]["total_broker_proof_gap"] == 264
    assert report["summary"]["safe_to_mutate_live"] is False
    assert report["bots"][0]["bot_id"] == "mnq_futures_sage"
    assert report["bots"][0]["retune_state"] == "STUCK_RESEARCH_FAILING"
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
                "priority_score": 1000.0,
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
                "priority_score": 1000.0,
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

    assert report["summary"]["n_broker_proof_ready"] == 1
    assert report["summary"]["n_broker_proof_shortfall"] == 0
    assert report["bots"][0]["broker_close_evidence"]["remaining_closed_trade_count"] == 0
    assert "broker close sample met (126/100)" in report["bots"][0]["next_action"]
    assert "independent research windows" in report["bots"][0]["next_action"]
