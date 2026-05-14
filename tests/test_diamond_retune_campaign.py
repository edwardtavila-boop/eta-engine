"""Tests for the broker-truth diamond retune campaign surface."""

from __future__ import annotations


def test_campaign_turns_retune_queue_into_safe_ranked_worklist() -> None:
    from eta_engine.scripts import diamond_retune_campaign as campaign

    audit = {
        "summary": {
            "n_bots": 14,
            "n_retune": 2,
            "safe_to_mutate_live": False,
            "scoring_basis": "broker_closed_trade_pnl_first",
        },
        "retune_queue": [
            {
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "strategy_kind": "orb_sage_gated",
                "asset_sleeve": "equity_index",
                "priority_score": 1061.81,
                "issue_code": "broker_pnl_negative",
                "worst_session": "overnight",
                "best_session": "close",
                "parameter_focus": ["overnight block", "sage_min_conviction"],
                "primary_experiment": "Paper-test blocking overnight entries.",
                "retune_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mnq_futures_sage --report-policy runtime"
                ),
                "live_mutation_policy": "paper_only_advisory",
                "safe_to_mutate_live": False,
            },
            {
                "bot_id": "mcl_sweep_reclaim",
                "symbol": "MCL1",
                "strategy_kind": "confluence_scorecard",
                "asset_sleeve": "metals_energy",
                "priority_score": 263.04,
                "issue_code": "broker_pnl_negative",
                "worst_session": "afternoon",
                "best_session": "overnight",
                "parameter_focus": ["event/session gate", "atr_stop_mult"],
                "primary_experiment": "Paper-test blocking afternoon entries.",
                "retune_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mcl_sweep_reclaim --report-policy runtime"
                ),
                "live_mutation_policy": "paper_only_advisory",
                "safe_to_mutate_live": False,
            },
        ],
    }

    report = campaign.build_campaign(audit, limit=1)

    assert report["kind"] == "eta_diamond_retune_campaign"
    assert report["summary"]["n_available_targets"] == 2
    assert report["summary"]["n_selected_targets"] == 1
    assert report["summary"]["top_bot"] == "mnq_futures_sage"
    assert report["summary"]["safe_to_mutate_live"] is False
    assert report["summary"]["execution_mode"] == "paper_research_only"
    assert report["targets"][0]["rank"] == 1
    assert report["targets"][0]["bot_id"] == "mnq_futures_sage"
    assert report["targets"][0]["next_command"].startswith("python -m eta_engine.scripts.run_research_grid")
    assert report["targets"][0]["promotion_block"] == "broker_proof_required"
    assert report["targets"][0]["live_mutation_policy"] == "paper_only_advisory"
    assert "no broker orders" in " ".join(report["safety_rails"]).lower()
