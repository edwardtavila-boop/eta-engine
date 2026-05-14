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
            "status": "research_passed_broker_proof_required",
            "exit_code": 0,
            "safe_to_mutate_live": False,
            "live_mutation_policy": "paper_only_advisory",
            "promotion_block": "broker_proof_required",
        },
    ]

    report = status.build_status(campaign=campaign, history_rows=history_rows)

    assert report["kind"] == "eta_diamond_retune_status"
    assert report["summary"]["n_targets"] == 3
    assert report["summary"]["n_attempted_bots"] == 2
    assert report["summary"]["n_unattempted_targets"] == 1
    assert report["summary"]["n_research_passed_broker_proof_required"] == 1
    assert report["summary"]["safe_to_mutate_live"] is False
    assert report["bots"][0]["bot_id"] == "mnq_futures_sage"
    assert report["bots"][0]["retune_state"] == "STUCK_RESEARCH_FAILING"
    assert report["bots"][0]["attempts"] == 3
    assert "new hypothesis" in report["bots"][0]["next_action"]
    assert report["bots"][1]["bot_id"] == "nq_futures_sage"
    assert report["bots"][1]["retune_state"] == "PASS_AWAITING_BROKER_PROOF"
    assert report["bots"][1]["safe_to_mutate_live"] is False
    assert report["bots"][2]["bot_id"] == "mcl_sweep_reclaim"
    assert report["bots"][2]["retune_state"] == "NOT_ATTEMPTED"
