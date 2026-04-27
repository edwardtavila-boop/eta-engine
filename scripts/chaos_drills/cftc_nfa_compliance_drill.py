"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.cftc_nfa_compliance_drill.

Drill: violate the CFTC / NFA pre-trade checklist; verify refusal.

What this drill asserts
-----------------------
:mod:`core.cftc_nfa_compliance` aggregates 9 rule checks behind
``check_compliance(ctx)``. A single ``BLOCKING`` violation must set
``passed=False`` and include the violating rule id in the result.

This drill builds five contexts, each deliberately breaking one
high-severity rule, and confirms:

* ``OWNS_ACCOUNT`` fires when the operator does not own the account.
* ``NO_EXTERNAL_CAPITAL`` fires when outside capital is detected.
* ``NO_POOL_MANAGEMENT`` fires on a non-operator deposit whitelist.
* ``APEX_NEWS_BLACKOUT`` fires during a news blackout window.
* A clean context passes all checks.

A silent regression in any of these would let an NFA-registration-
triggering pattern reach the venue -- the worst class of bug we can
ship.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.core.cftc_nfa_compliance import (
    ComplianceRuleId,
    PreTradeContext,
    Severity,
    check_compliance,
)
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_cftc_nfa_compliance"]


def _ctx(**overrides: object) -> PreTradeContext:
    base: dict[str, Any] = {
        "operator_owned_account": True,
        "account_id": "apex-op-001",
        "symbol": "MNQ",
        "side": "BUY",
        "qty": 1.0,
        "external_capital_present": False,
        "deposit_whitelist": ["apex-op-001"],
        "eta_account_id": None,
        "other_eta_account_ids": [],
        "has_opposing_apex_position": False,
        "open_order_ids_same_symbol": [],
        "order_cancel_rate_hz": 0.0,
        "bid_ask_self_overlap": False,
        "news_blackout_active": False,
        "is_promotional_communication": False,
        "promotional_disclaimer_included": False,
    }
    base.update(overrides)
    return PreTradeContext(**base)


def drill_cftc_nfa_compliance(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001
    """Exercise 4 BLOCKING violations + a clean context; confirm the verdicts."""
    failing_cases: dict[str, tuple[PreTradeContext, ComplianceRuleId]] = {
        "owns_account": (_ctx(operator_owned_account=False), ComplianceRuleId.OWNS_ACCOUNT),
        "external_cap": (_ctx(external_capital_present=True), ComplianceRuleId.NO_EXTERNAL_CAPITAL),
        "pool_mgmt": (
            _ctx(deposit_whitelist=["apex-op-001", "someone-else-xyz"]),
            ComplianceRuleId.NO_POOL_MANAGEMENT,
        ),
        "news_blackout": (
            _ctx(eta_account_id="apex-eval-01", news_blackout_active=True),
            ComplianceRuleId.APEX_NEWS_BLACKOUT,
        ),
    }
    observed: dict[str, Any] = {}
    for case_name, (ctx, expected_rule) in failing_cases.items():
        result = check_compliance(ctx)
        if result.passed:
            return drill_result(
                "cftc_nfa_compliance",
                passed=False,
                details=f"{case_name}: compliance incorrectly passed with {expected_rule.value} active",
            )
        rule_ids = {v.rule for v in result.violations if v.severity == Severity.BLOCKING}
        if expected_rule not in rule_ids:
            return drill_result(
                "cftc_nfa_compliance",
                passed=False,
                details=(
                    f"{case_name}: expected BLOCKING rule {expected_rule.value} but got {[r.value for r in rule_ids]}"
                ),
            )
        observed[case_name] = {
            "passed": result.passed,
            "rule": expected_rule.value,
            "n_block": len(rule_ids),
        }

    clean = check_compliance(_ctx())
    if not clean.passed:
        return drill_result(
            "cftc_nfa_compliance",
            passed=False,
            details=f"clean context incorrectly failed: {clean.violations!r}",
        )
    observed["clean_context"] = {"passed": True, "n_violations": len(clean.violations)}

    return drill_result(
        "cftc_nfa_compliance",
        passed=True,
        details="4 BLOCKING violations refused and clean context passed",
        observed=observed,
    )
