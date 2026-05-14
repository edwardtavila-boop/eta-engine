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


def test_runner_executes_allowed_registry_research_and_keeps_live_locked(tmp_path) -> None:
    from eta_engine.scripts import diamond_retune_runner as runner

    campaign = {
        "kind": "eta_diamond_retune_campaign",
        "generated_at_utc": "2026-05-14T20:00:00+00:00",
        "summary": {"execution_mode": "paper_research_only", "safe_to_mutate_live": False},
        "targets": [
            {
                "rank": 1,
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "asset_sleeve": "equity_index",
                "priority_score": 1061.81,
                "next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mnq_futures_sage --report-policy runtime"
                ),
                "promotion_block": "broker_proof_required",
                "live_mutation_policy": "paper_only_advisory",
                "safe_to_mutate_live": False,
            },
        ],
    }
    seen: list[list[str]] = []

    def fake_executor(args: list[str], *, timeout_seconds: int) -> runner.CommandResult:
        seen.append(args)
        assert timeout_seconds == 123
        return runner.CommandResult(returncode=0, stdout="PASS-ish research", stderr="")

    receipt = runner.run_campaign_once(
        campaign,
        out_path=tmp_path / "receipt.json",
        timeout_seconds=123,
        executor=fake_executor,
    )

    assert seen == [
        [
            runner.PYTHON_EXE,
            "-m",
            "eta_engine.scripts.run_research_grid",
            "--source",
            "registry",
            "--bots",
            "mnq_futures_sage",
            "--report-policy",
            "runtime",
        ]
    ]
    assert receipt["kind"] == "eta_diamond_retune_runner"
    assert receipt["selected_target"]["bot_id"] == "mnq_futures_sage"
    assert receipt["status"] == "research_passed_broker_proof_required"
    assert receipt["safe_to_mutate_live"] is False
    assert receipt["live_mutation_policy"] == "paper_only_advisory"
    assert receipt["promotion_block"] == "broker_proof_required"
    assert (tmp_path / "receipt.json").exists()


def test_runner_rejects_non_registry_or_live_mutating_commands() -> None:
    from eta_engine.scripts import diamond_retune_runner as runner

    target = {
        "bot_id": "bad",
        "next_command": "python -m eta_engine.scripts.place_order --symbol MNQ",
        "live_mutation_policy": "paper_only_advisory",
        "safe_to_mutate_live": False,
    }

    try:
        runner.command_args_for_target(target)
    except ValueError as exc:
        assert "allowed registry research command" in str(exc)
    else:  # pragma: no cover - defensive branch
        raise AssertionError("unsafe command was accepted")


def test_runner_records_research_failure_as_keep_retuning(tmp_path) -> None:
    from eta_engine.scripts import diamond_retune_runner as runner

    campaign = {
        "targets": [
            {
                "rank": 1,
                "bot_id": "nq_futures_sage",
                "symbol": "NQ1",
                "asset_sleeve": "equity_index",
                "priority_score": 822.95,
                "next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots nq_futures_sage --report-policy runtime"
                ),
                "promotion_block": "broker_proof_required",
                "live_mutation_policy": "paper_only_advisory",
                "safe_to_mutate_live": False,
            },
        ],
    }

    def fake_executor(args: list[str], *, timeout_seconds: int) -> runner.CommandResult:
        return runner.CommandResult(returncode=1, stdout="no pass", stderr="")

    receipt = runner.run_campaign_once(
        campaign,
        out_path=tmp_path / "receipt.json",
        executor=fake_executor,
    )

    assert receipt["exit_code"] == 1
    assert receipt["status"] == "research_failed_keep_retuning"
    assert receipt["promotion_block"] == "broker_proof_required"
    assert receipt["safe_to_mutate_live"] is False


def test_runner_records_timeout_as_keep_retuning(tmp_path) -> None:
    from eta_engine.scripts import diamond_retune_runner as runner

    campaign = {
        "targets": [
            {
                "rank": 1,
                "bot_id": "mnq_futures_sage",
                "symbol": "MNQ1",
                "asset_sleeve": "equity_index",
                "priority_score": 1061.81,
                "next_command": (
                    "python -m eta_engine.scripts.run_research_grid "
                    "--source registry --bots mnq_futures_sage --report-policy runtime"
                ),
                "promotion_block": "broker_proof_required",
                "live_mutation_policy": "paper_only_advisory",
                "safe_to_mutate_live": False,
            },
        ],
    }

    def timeout_executor(args: list[str], *, timeout_seconds: int) -> runner.CommandResult:
        raise TimeoutError("simulated timeout")

    receipt = runner.run_campaign_once(
        campaign,
        out_path=tmp_path / "receipt.json",
        executor=timeout_executor,
    )

    assert receipt["exit_code"] == 124
    assert receipt["status"] == "research_timeout_keep_retuning"
    assert receipt["safe_to_mutate_live"] is False
