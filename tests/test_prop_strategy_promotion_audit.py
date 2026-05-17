"""Tests for the prop strategy promotion audit artifact."""

from __future__ import annotations

from eta_engine.scripts import prop_strategy_promotion_audit as audit


def _check(name: str, status: str, detail: str = "detail", **evidence: object) -> dict[str, object]:
    payload: dict[str, object] = {"name": name, "status": status, "detail": detail}
    if evidence:
        payload["evidence"] = evidence
    return payload


def _candidate(
    *,
    live_routing_allowed: bool = False,
    can_live_trade: bool = False,
    launch_lane: str = "paper_soak",
    blockers: list[str] | None = None,
) -> dict[str, object]:
    return {
        "bot_id": "volume_profile_mnq",
        "role": "primary",
        "symbol": "MNQ1",
        "launch_lane": launch_lane,
        "can_live_trade": can_live_trade,
        "live_routing_allowed": live_routing_allowed,
        "evidence_grade": "strict_pass",
        "strict_gate": {"trades": 2916, "sh_def": 2.91, "L": True, "S": True},
        "blockers": blockers if blockers is not None else ["bot row is not can_live_trade"],
    }


def _runner_candidate(
    *,
    bot_id: str = "volume_profile_nq",
    symbol: str = "NQ1",
    evidence_grade: str = "near_strict",
    blockers: list[str] | None = None,
) -> dict[str, object]:
    return {
        "bot_id": bot_id,
        "role": "runner",
        "symbol": symbol,
        "launch_lane": "paper_soak",
        "active": True,
        "can_paper_trade": True,
        "can_live_trade": False,
        "live_routing_allowed": False,
        "evidence_grade": evidence_grade,
        "strict_gate": {"trades": 1284, "sh_def": 1.74, "L": True, "S": True},
        "blockers": blockers if blockers is not None else ["runner slot is paper/research only"],
    }


def _closed_trade_ledger(
    *,
    bot_id: str = "volume_profile_nq",
    closed_trade_count: int = 0,
    cumulative_r: float = 9.25,
    profit_factor: float = 1.42,
    total_realized_pnl: float = 825.0,
    win_rate_pct: float = 56.0,
) -> dict[str, object]:
    per_bot: dict[str, object] = {}
    if closed_trade_count:
        per_bot[bot_id] = {
            "closed_trade_count": closed_trade_count,
            "cumulative_r": cumulative_r,
            "profit_factor": profit_factor,
            "total_realized_pnl": total_realized_pnl,
            "win_rate_pct": win_rate_pct,
        }
    return {"per_bot": per_bot}


def _supervisor_heartbeat(
    *,
    bot_id: str = "volume_profile_nq",
    last_bar_ts: str = "2026-05-15T03:55:45+00:00",
    last_signal_at: str = "",
) -> dict[str, object]:
    return {
        "ts": "2026-05-15T03:55:48+00:00",
        "bots": [
            {
                "bot_id": bot_id,
                "mode": "paper_live",
                "entry_enabled": True,
                "last_bar_ts": last_bar_ts,
                "last_bar_close": 29565.0,
                "last_signal_at": last_signal_at,
                "n_entries": 0,
                "n_exits": 0,
                "consecutive_broker_rejects": 0,
            },
        ],
    }


def _shadow_signals(*, bot_id: str = "volume_profile_nq", count: int = 2) -> list[dict[str, object]]:
    return [
        {
            "ts": f"2026-05-15T03:5{i}:00+00:00",
            "bot_id": bot_id,
            "signal_id": f"{bot_id}_{i}",
            "symbol": "NQ1",
            "side": "BUY" if i % 2 else "SELL",
            "lifecycle": "EVAL_PAPER",
            "route_target": "paper",
            "route_reason": "lifecycle_eval_paper",
        }
        for i in range(count)
    ]


def _shadow_outcome_report(
    *,
    bot_id: str = "volume_profile_nq",
    evaluated_count: int = 33,
    shadow_signal_count: int | None = None,
    missing_bars: int = 0,
    missing_bar_datasets: int = 0,
    no_bar_after_signal: int = 0,
    missing_context: int = 0,
    latest_bar_coverage_end_ts: str = "",
    verdict: str = "POSITIVE_COUNTERFACTUAL_EDGE",
) -> dict[str, object]:
    shadow_signal_count = evaluated_count if shadow_signal_count is None else shadow_signal_count
    return {
        "summary": {
            "status": "COUNTERFACTUAL_EDGE_SEEN",
            "broker_backed": False,
            "promotion_proof": False,
            "truth_note": "Counterfactual shadow replay only; not broker-backed closed-trade PnL.",
        },
        "per_bot": {
            bot_id: {
                "bot_id": bot_id,
                "shadow_signal_count": shadow_signal_count,
                "evaluated_count": evaluated_count,
                "missing_bars": missing_bars,
                "missing_bar_datasets": missing_bar_datasets,
                "no_bar_after_signal": no_bar_after_signal,
                "missing_context": missing_context,
                "latest_bar_coverage_end_ts": latest_bar_coverage_end_ts,
                "avg_r": 0.18,
                "total_r": 5.94,
                "profit_factor": 1.62,
                "win_rate_pct": 57.58,
                "verdict": verdict,
                "broker_backed": False,
                "promotion_proof": False,
            },
        },
    }


def _failed_research_retest(*, bot_id: str = "volume_profile_nq") -> dict[str, object]:
    return {
        bot_id: {
            "source": "research_grid_runtime",
            "bot_id": bot_id,
            "windows": 1,
            "positive_oos_windows": 0,
            "oos_sharpe": -3.704,
            "dsr_pass_fraction": 0.0,
            "verdict": "FAIL",
            "result_status": "fail",
            "artifact_class": "low_signal",
            "report_path": r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\research_grid\research_grid_fail.md",
        },
    }


def test_runner_up_candidate_names_missing_shadow_context_when_old_signals_cannot_replay() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate()],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat=_supervisor_heartbeat(last_signal_at=""),
        shadow_signals=_shadow_signals(count=40),
        shadow_outcome_report=_shadow_outcome_report(
            evaluated_count=0,
            shadow_signal_count=40,
            missing_context=40,
            verdict="NO_EVALUATED_SIGNALS",
        ),
    )

    evidence = report["next_runner_candidate"]["shadow_outcome_evidence"]

    assert evidence["missing_context"] == 40
    assert "fresh bracket-context shadow signals" in report["next_runner_candidate"]["next_action"]
    assert "older shadow signals lack planned entry/risk context" in report["next_runner_candidate"]["operator_note"]


def test_promotion_audit_explains_paper_soak_hold_with_required_evidence() -> None:
    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check(
                    "primary_ladder",
                    "BLOCKED",
                    "volume_profile_mnq is not cleared by the futures prop ladder",
                    primary_candidate=_candidate(),
                ),
                _check(
                    "prop_readiness",
                    "BLOCKED",
                    "prop readiness is BLOCKED",
                    missing_secrets=["BLUSKY_TRADOVATE_CID"],
                ),
                _check("broker_native_brackets", "BLOCKED", "manual OCO proof required"),
                _check("closed_trade_ledger", "PASS", "ledger ready", closed_trade_count=43000),
                _check(
                    "live_bot_gate",
                    "BLOCKED",
                    "volume_profile_mnq visible but not marked can_live_trade",
                    launch_lane="paper_soak",
                ),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [_candidate()],
        },
    )

    assert report["summary"] == "BLOCKED_PAPER_SOAK"
    assert report["primary_bot"] == "volume_profile_mnq"
    assert report["primary"]["strict_gate_status"] == "PASS"
    assert report["primary"]["launch_lane"] == "paper_soak"
    assert report["primary"]["can_live_trade"] is False
    assert report["readiness"]["prop_readiness"] == "BLOCKED"
    assert report["readiness"]["broker_native_brackets"] == "BLOCKED"
    assert report["readiness"]["closed_trade_ledger"] == "PASS"
    assert report["ready_for_prop_dry_run_review"] is False
    assert (
        "set volume_profile_mnq can_live_trade=true only after paper-soak promotion approval"
        in report["required_evidence"]
    )
    assert "clear prop_readiness to PASS / READY_FOR_DRY_RUN" in report["required_evidence"]
    assert "clear broker_native_brackets to PASS" in report["required_evidence"]


def test_promotion_audit_marks_ready_when_primary_and_gate_are_clear() -> None:
    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "READY_FOR_CONTROLLED_PROP_DRY_RUN",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "PASS", primary_bot="volume_profile_mnq"),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "PASS"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "PRIMARY_READY_FOR_CONTROLLED_PROP_DRY_RUN"},
            "candidates": [
                _candidate(
                    live_routing_allowed=True,
                    can_live_trade=True,
                    launch_lane="prop_dry_run",
                    blockers=[],
                ),
            ],
        },
    )

    assert report["summary"] == "READY_FOR_PROP_DRY_RUN_REVIEW"
    assert report["ready_for_prop_dry_run_review"] is True
    assert report["required_evidence"] == []
    assert report["primary"]["live_routing_allowed"] is True
    assert report["scope_family"] == "futures_prop_ladder"
    assert report["scope_mode"] == "controlled_prop_dry_run"
    assert report["scope_summary"] == "futures_prop_ladder/controlled_prop_dry_run for volume_profile_mnq"
    assert report["parallel_launch_surface"] == "eta_engine.scripts.prop_launch_check"
    assert report["parallel_launch_scope"] == "diamond_wave25_launch_readiness"
    assert report["parallel_launch_command"] == "python -m eta_engine.scripts.prop_launch_check --json"
    assert (
        report["parallel_lane_hint"]
        == "Separate lane: diamond_wave25_launch_readiness via eta_engine.scripts.prop_launch_check"
    )


def test_promotion_audit_blocks_kaizen_retired_primary_without_reactivation_hint() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
        "deactivation_reason": "tier=DECAY mc=MIXED expR=-0.0061 n=66",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check(
                    "primary_ladder",
                    "BLOCKED",
                    "volume_profile_mnq is not cleared by the futures prop ladder",
                    primary_candidate=candidate,
                ),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check(
                    "live_bot_gate",
                    "BLOCKED",
                    "volume_profile_mnq is deactivated on the live readiness surface",
                ),
            ],
        },
        ladder_report={"summary": {"automation_mode": "PROP_DRY_RUN_READY_LIVE_BLOCKED"}, "candidates": [candidate]},
    )

    required = "\n".join(report["required_evidence"])

    assert report["summary"] == "BLOCKED_KAIZEN_RETIRED"
    assert report["primary"]["active"] is False
    assert report["primary"]["deactivation_source"] == "kaizen_sidecar"
    assert "review Kaizen retirement evidence" in required
    assert "set volume_profile_mnq can_live_trade=true" not in required


def test_kaizen_retired_primary_surfaces_runner_up_candidate_without_promoting() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
        "deactivation_reason": "tier=DECAY mc=MIXED expR=-0.0061 n=66",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check(
                    "primary_ladder",
                    "BLOCKED",
                    "volume_profile_mnq is not cleared by the futures prop ladder",
                    primary_candidate=candidate,
                ),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate(), _runner_candidate(bot_id="rsi_mr_mnq_v2", symbol="MNQ1")],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat=_supervisor_heartbeat(),
        shadow_signals=[],
    )

    required = "\n".join(report["required_evidence"])

    assert report["summary"] == "BLOCKED_KAIZEN_RETIRED"
    assert report["runner_up_count"] == 2
    assert report["next_runner_candidate"]["bot_id"] == "volume_profile_nq"
    assert report["next_runner_candidate"]["can_live_trade"] is False
    assert report["next_runner_candidate"]["strict_gate_status"] == "WATCH"
    assert report["next_runner_candidate"]["broker_close_evidence"]["closed_trade_count"] == 0
    assert report["next_runner_candidate"]["broker_close_evidence"]["verdict"] == "MISSING_BROKER_CLOSES"
    assert report["next_runner_candidate"]["supervisor_watch_evidence"]["watched"] is True
    assert report["next_runner_candidate"]["supervisor_watch_evidence"]["verdict"] == "WATCHING_NO_SIGNAL_YET"
    assert report["next_runner_candidate"]["shadow_signal_evidence"]["signal_count"] == 0
    assert report["next_runner_candidate"]["next_action"].startswith("Keep volume_profile_nq in paper watch")
    assert report["runner_up_candidates"][1]["bot_id"] == "rsi_mr_mnq_v2"
    assert "collect broker-backed closes for runner-up candidate volume_profile_nq" in required
    assert "focus runner-up review on volume_profile_nq" in report["operator_note"]


def test_runner_up_candidate_includes_positive_broker_close_evidence_when_present() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate()],
        },
        closed_trade_ledger=_closed_trade_ledger(closed_trade_count=42),
    )

    evidence = report["next_runner_candidate"]["broker_close_evidence"]

    assert evidence["closed_trade_count"] == 42
    assert evidence["verdict"] == "POSITIVE_BROKER_CLOSE_EVIDENCE"
    assert "evaluate runner-up candidate volume_profile_nq in paper soak" in "\n".join(
        report["required_evidence"],
    )


def test_runner_up_candidate_surfaces_when_not_watched_by_supervisor() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate()],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat={"bots": []},
        shadow_signals=[],
    )

    watch = report["next_runner_candidate"]["supervisor_watch_evidence"]

    assert watch["watched"] is False
    assert watch["verdict"] == "NOT_WATCHED_BY_SUPERVISOR"
    assert report["next_runner_candidate"]["next_action"].startswith("Wire volume_profile_nq")


def test_runner_up_candidate_surfaces_shadow_signals_without_closes() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate()],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat=_supervisor_heartbeat(last_signal_at=""),
        shadow_signals=_shadow_signals(count=3),
    )

    evidence = report["next_runner_candidate"]["shadow_signal_evidence"]

    assert evidence["signal_count"] == 3
    assert evidence["verdict"] == "SHADOW_PAPER_SIGNALS_SEEN"
    assert evidence["route_targets"] == {"paper": 3}
    assert report["next_runner_candidate"]["next_action"].startswith("Convert volume_profile_nq shadow signals")
    assert "closed outcomes" in report["next_runner_candidate"]["operator_note"]


def test_runner_up_candidate_surfaces_shadow_outcomes_without_promoting() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate()],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat=_supervisor_heartbeat(last_signal_at=""),
        shadow_signals=_shadow_signals(count=40),
        shadow_outcome_report=_shadow_outcome_report(),
    )

    evidence = report["next_runner_candidate"]["shadow_outcome_evidence"]

    assert evidence["evaluated_count"] == 33
    assert evidence["verdict"] == "POSITIVE_COUNTERFACTUAL_EDGE"
    assert evidence["broker_backed"] is False
    assert evidence["promotion_proof"] is False
    assert report["next_runner_candidate"]["broker_close_evidence"]["closed_trade_count"] == 0
    assert "broker-paper close capture" in report["next_runner_candidate"]["next_action"]
    assert "not broker proof" in report["next_runner_candidate"]["operator_note"]


def test_runner_up_candidate_emits_safe_retune_plan_when_shadow_replay_is_weak() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate()],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat=_supervisor_heartbeat(last_signal_at=""),
        shadow_signals=_shadow_signals(count=88),
        shadow_outcome_report=_shadow_outcome_report(
            evaluated_count=88,
            shadow_signal_count=88,
            verdict="WEAK_OR_NEGATIVE_COUNTERFACTUAL",
        ),
    )

    runner = report["next_runner_candidate"]
    plan = runner["retune_plan"]

    assert runner["next_action"].startswith("Retune volume_profile_nq")
    assert plan["status"] == "PAPER_ONLY_RETUNE_REQUIRED"
    assert plan["trigger"] == "weak_shadow_replay"
    assert plan["retune_command"] == (
        "python -m eta_engine.scripts.run_research_grid "
        "--source registry --bots volume_profile_nq --report-policy runtime"
    )
    assert plan["bar_refresh_task"] == "ETA-IndexFutures-Bar-Refresh"
    assert "refresh_index_futures_bars.py" in plan["bar_refresh_command"]
    assert plan["safe_to_mutate_live"] is False
    assert plan["broker_backed"] is False
    assert plan["promotion_proof"] is False


def test_research_retest_failure_rotates_next_runner_off_failed_candidate() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [
                candidate,
                _runner_candidate(),
                _runner_candidate(bot_id="rsi_mr_mnq_v2", symbol="MNQ1"),
            ],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat=_supervisor_heartbeat(last_signal_at=""),
        shadow_signals=_shadow_signals(count=88),
        shadow_outcome_report=_shadow_outcome_report(
            evaluated_count=88,
            shadow_signal_count=88,
            verdict="WEAK_OR_NEGATIVE_COUNTERFACTUAL",
        ),
        research_retest_rows=_failed_research_retest(),
    )

    failed_runner = report["runner_up_candidates"][0]
    retune_plan = failed_runner["retune_plan"]

    assert failed_runner["bot_id"] == "volume_profile_nq"
    assert failed_runner["research_retest_evidence"]["verdict"] == "FAIL"
    assert failed_runner["research_retest_evidence"]["hard_fail"] is True
    assert retune_plan["status"] == "PAPER_ONLY_RETUNE_FAILED"
    assert retune_plan["safe_to_mutate_live"] is False
    assert report["next_runner_candidate"]["bot_id"] == "rsi_mr_mnq_v2"
    assert "volume_profile_nq" not in "\n".join(report["required_evidence"])
    assert "focus runner-up review on rsi_mr_mnq_v2" in report["operator_note"]


def test_no_reviewable_runner_when_remaining_candidates_have_negative_small_samples() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [
                candidate,
                _runner_candidate(),
                _runner_candidate(bot_id="rsi_mr_mnq_v2", symbol="MNQ1"),
                _runner_candidate(bot_id="mym_sweep_reclaim", symbol="MYM1"),
            ],
        },
        closed_trade_ledger={
            "per_bot": {
                "rsi_mr_mnq_v2": {
                    "closed_trade_count": 9,
                    "cumulative_r": -6.1301,
                    "profit_factor": 0.2273,
                    "total_realized_pnl": -212.5,
                    "win_rate_pct": 33.33,
                },
                "mym_sweep_reclaim": {
                    "closed_trade_count": 14,
                    "cumulative_r": -6.3887,
                    "profit_factor": 0.2491,
                    "total_realized_pnl": -108.5,
                    "win_rate_pct": 35.71,
                },
            },
        },
        supervisor_heartbeat=_supervisor_heartbeat(last_signal_at=""),
        shadow_signals=_shadow_signals(count=88),
        shadow_outcome_report=_shadow_outcome_report(
            evaluated_count=88,
            shadow_signal_count=88,
            verdict="WEAK_OR_NEGATIVE_COUNTERFACTUAL",
        ),
        research_retest_rows=_failed_research_retest(),
    )

    required = "\n".join(report["required_evidence"])

    assert report["next_runner_candidate"] == {}
    assert report["runner_up_candidates"][1]["broker_close_evidence"]["verdict"] == "EARLY_NEGATIVE_BROKER_SAMPLE"
    assert report["runner_up_candidates"][2]["broker_close_evidence"]["verdict"] == "EARLY_NEGATIVE_BROKER_SAMPLE"
    assert "no runner-up candidate is promotion-reviewable" in required
    assert "evaluate runner-up candidates" in report["operator_note"]


def test_runner_up_candidate_names_stale_replay_coverage_when_shadow_outcome_replay_cannot_evaluate() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate()],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat=_supervisor_heartbeat(last_signal_at=""),
        shadow_signals=_shadow_signals(count=40),
        shadow_outcome_report=_shadow_outcome_report(
            evaluated_count=0,
            shadow_signal_count=40,
            missing_bars=40,
            no_bar_after_signal=40,
            latest_bar_coverage_end_ts="2026-05-08T10:50:00+00:00",
            verdict="NO_EVALUATED_SIGNALS",
        ),
    )

    evidence = report["next_runner_candidate"]["shadow_outcome_evidence"]

    assert evidence["shadow_signal_count"] == 40
    assert evidence["evaluated_count"] == 0
    assert evidence["missing_bars"] == 40
    assert evidence["no_bar_after_signal"] == 40
    assert evidence["latest_bar_coverage_end_ts"] == "2026-05-08T10:50:00+00:00"
    assert "Refresh NQ1 5-minute replay bars" in report["next_runner_candidate"]["next_action"]
    assert "2026-05-08T10:50:00+00:00" in report["next_runner_candidate"]["next_action"]
    assert "local NQ1 5-minute replay bars end at 2026-05-08T10:50:00+00:00" in (
        report["next_runner_candidate"]["operator_note"]
    )


def test_runner_up_candidate_names_missing_shadow_context_before_replay() -> None:
    candidate = {
        **_candidate(launch_lane="deactivated", blockers=["bot row is deactivated via kaizen_sidecar"]),
        "active": False,
        "data_status": "deactivated",
        "promotion_status": "deactivated",
        "deactivation_source": "kaizen_sidecar",
    }

    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=candidate),
                _check("prop_readiness", "PASS"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check("live_bot_gate", "BLOCKED", "volume_profile_mnq is deactivated"),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [candidate, _runner_candidate()],
        },
        closed_trade_ledger=_closed_trade_ledger(),
        supervisor_heartbeat=_supervisor_heartbeat(last_signal_at=""),
        shadow_signals=_shadow_signals(count=40),
        shadow_outcome_report=_shadow_outcome_report(
            evaluated_count=0,
            shadow_signal_count=40,
            missing_context=40,
            verdict="NO_EVALUATED_SIGNALS",
        ),
    )

    evidence = report["next_runner_candidate"]["shadow_outcome_evidence"]

    assert evidence["missing_context"] == 40
    assert "fresh bracket-context shadow signals" in report["next_runner_candidate"]["next_action"]
    assert "older shadow signals lack planned entry/risk context" in report["next_runner_candidate"]["operator_note"]


def test_promotion_audit_prefers_live_gate_deactivation_over_stale_ladder_candidate() -> None:
    report = audit.build_promotion_audit_report(
        gate_report={
            "summary": "BLOCKED",
            "primary_bot": "volume_profile_mnq",
            "checks": [
                _check("primary_ladder", "BLOCKED", primary_candidate=_candidate()),
                _check("prop_readiness", "BLOCKED"),
                _check("broker_native_brackets", "PASS"),
                _check("closed_trade_ledger", "PASS", closed_trade_count=43000),
                _check(
                    "live_bot_gate",
                    "BLOCKED",
                    "volume_profile_mnq is deactivated on the live readiness surface",
                    live_readiness_found=True,
                    live_readiness_active=False,
                    live_readiness_launch_lane="deactivated",
                    live_readiness_data_status="deactivated",
                    live_readiness_promotion_status="deactivated",
                    live_readiness_deactivation_source="kaizen_sidecar",
                    live_readiness_deactivation_reason="tier=DECAY mc=MIXED expR=-0.0061 n=66",
                ),
            ],
        },
        ladder_report={
            "summary": {"automation_mode": "FULLY_AUTOMATED_PAPER_PROP_HELD"},
            "candidates": [_candidate()],
        },
    )

    required = "\n".join(report["required_evidence"])

    assert report["summary"] == "BLOCKED_KAIZEN_RETIRED"
    assert report["primary"]["active"] is False
    assert report["primary"]["launch_lane"] == "deactivated"
    assert report["primary"]["deactivation_source"] == "kaizen_sidecar"
    assert "review Kaizen retirement evidence" in required
    assert "set volume_profile_mnq can_live_trade=true" not in required
