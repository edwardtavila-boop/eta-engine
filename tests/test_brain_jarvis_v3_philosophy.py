from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.brain.jarvis_v3.philosophy import (
    DOCTRINE,
    PRIORITY_ORDER,
    Tenet,
    apply_doctrine,
    kaizen_pre_condition,
    summarize_doctrine,
)


def test_apply_doctrine_downgrades_bot_risk_for_capital_and_autopilot_tenets() -> None:
    now = datetime(2026, 4, 29, tzinfo=UTC)

    verdict = apply_doctrine(
        proposed_verdict="APPROVED",
        subsystem="bot.mnq",
        action="ORDER_PLACE",
        now=now,
    )

    assert verdict.ts == now
    assert verdict.doctrine_verdict == "CONDITIONAL"
    assert verdict.net_bias < -0.3
    assert Tenet.CAPITAL_FIRST.value in verdict.tenets_applied
    assert Tenet.NEVER_ON_AUTOPILOT.value in verdict.tenets_applied


def test_apply_doctrine_defaults_unknown_verdict_to_conditional_before_bias() -> None:
    verdict = apply_doctrine(
        proposed_verdict="NOT_A_VERDICT",
        subsystem="firm.pm",
        action="review",
    )

    assert verdict.proposed_verdict == "NOT_A_VERDICT"
    assert verdict.doctrine_verdict == "CONDITIONAL"
    assert verdict.net_bias > 0


def test_kaizen_pre_condition_and_summary_surface_doctrine_contract() -> None:
    ok, ok_reason = kaizen_pre_condition(7)
    breached, breached_reason = kaizen_pre_condition(3)
    summary = summarize_doctrine()

    assert ok is True
    assert "KAIZEN honored" in ok_reason
    assert breached is False
    assert "KAIZEN breached" in breached_reason
    assert list(PRIORITY_ORDER)[0] is Tenet.CAPITAL_FIRST
    assert DOCTRINE[Tenet.OBSERVABILITY].violation_code == "audit_gap"
    assert "EVOLUTIONARY TRADING ALGO DOCTRINE" in summary
