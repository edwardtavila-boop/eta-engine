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


def _closed_trade_ledger(*, bot_id: str = "volume_profile_nq", closed_trade_count: int = 0) -> dict[str, object]:
    per_bot: dict[str, object] = {}
    if closed_trade_count:
        per_bot[bot_id] = {
            "closed_trade_count": closed_trade_count,
            "cumulative_r": 9.25,
            "profit_factor": 1.42,
            "total_realized_pnl": 825.0,
            "win_rate_pct": 56.0,
        }
    return {"per_bot": per_bot}


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
    )

    required = "\n".join(report["required_evidence"])

    assert report["summary"] == "BLOCKED_KAIZEN_RETIRED"
    assert report["runner_up_count"] == 2
    assert report["next_runner_candidate"]["bot_id"] == "volume_profile_nq"
    assert report["next_runner_candidate"]["can_live_trade"] is False
    assert report["next_runner_candidate"]["strict_gate_status"] == "WATCH"
    assert report["next_runner_candidate"]["broker_close_evidence"]["closed_trade_count"] == 0
    assert report["next_runner_candidate"]["broker_close_evidence"]["verdict"] == "MISSING_BROKER_CLOSES"
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
