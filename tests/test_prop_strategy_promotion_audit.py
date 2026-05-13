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
