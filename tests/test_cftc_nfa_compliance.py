"""Tests for eta_engine.core.cftc_nfa_compliance."""

from __future__ import annotations

import pytest

from eta_engine.core.cftc_nfa_compliance import (
    ComplianceCheckResult,
    ComplianceRuleId,
    ComplianceViolation,
    PreTradeContext,
    Severity,
    check_compliance,
)


def _ok_ctx(**overrides: object) -> PreTradeContext:
    """Baseline compliant context; tests apply narrow overrides."""
    base = {
        "operator_owned_account": True,
        "account_id": "apex-main",
        "symbol": "MNQH6",
        "side": "BUY",
        "qty": 1.0,
        "external_capital_present": False,
        "deposit_whitelist": [],
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
    return PreTradeContext(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Positive path
# --------------------------------------------------------------------------- #


def test_clean_context_passes() -> None:
    res = check_compliance(_ok_ctx())
    assert res.passed is True
    assert res.violations == []


def test_advisory_only_still_passes() -> None:
    # Cancel rate 2Hz is ADVISORY -- does not block
    res = check_compliance(_ok_ctx(order_cancel_rate_hz=2.5))
    assert res.passed is True
    assert len(res.violations) == 1
    assert res.violations[0].severity == Severity.ADVISORY
    assert res.violations[0].rule == ComplianceRuleId.NO_LAYER_CANCEL


# --------------------------------------------------------------------------- #
# Blocking CFTC rules
# --------------------------------------------------------------------------- #


def test_trading_for_others_blocks() -> None:
    res = check_compliance(_ok_ctx(operator_owned_account=False))
    assert res.passed is False
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.OWNS_ACCOUNT in rules


def test_external_capital_blocks() -> None:
    res = check_compliance(_ok_ctx(external_capital_present=True))
    assert res.passed is False
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.NO_EXTERNAL_CAPITAL in rules


def test_third_party_deposit_whitelist_blocks() -> None:
    res = check_compliance(
        _ok_ctx(
            account_id="apex-main",
            deposit_whitelist=["apex-main", "grandma-bob"],
        )
    )
    assert res.passed is False
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.NO_POOL_MANAGEMENT in rules


def test_whitelist_that_is_empty_or_only_self_passes() -> None:
    # Empty list
    assert check_compliance(_ok_ctx()).passed is True
    # Only operator in whitelist
    assert (
        check_compliance(
            _ok_ctx(
                account_id="apex-main",
                deposit_whitelist=["apex-main"],
            )
        ).passed
        is True
    )


def test_self_match_blocks() -> None:
    res = check_compliance(_ok_ctx(bid_ask_self_overlap=True))
    assert res.passed is False
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.NO_SELF_MATCH in rules


# --------------------------------------------------------------------------- #
# Apex prop-firm rules
# --------------------------------------------------------------------------- #


def test_multiple_eta_accounts_is_advisory_not_blocking() -> None:
    # ApexRule ONE_ACCOUNT_PER_TRADE is ADVISORY, not BLOCKING -- operator
    # may have a second Apex account for unrelated symbols.
    res = check_compliance(
        _ok_ctx(
            eta_account_id="apex-1",
            other_eta_account_ids=["apex-2"],
        )
    )
    assert res.passed is True
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.APEX_ONE_ACCOUNT in rules
    # Confirm severity is ADVISORY for this particular rule
    v = next(v for v in res.violations if v.rule == ComplianceRuleId.APEX_ONE_ACCOUNT)
    assert v.severity == Severity.ADVISORY


def test_opposing_apex_position_blocks() -> None:
    res = check_compliance(
        _ok_ctx(
            eta_account_id="apex-1",
            has_opposing_apex_position=True,
        )
    )
    assert res.passed is False
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.APEX_NO_CROSS_HEDGE in rules


def test_news_blackout_blocks() -> None:
    res = check_compliance(_ok_ctx(news_blackout_active=True))
    assert res.passed is False
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.APEX_NEWS_BLACKOUT in rules


def test_apex_checks_skipped_when_not_eta_account() -> None:
    # No eta_account_id set -> Apex rules do not fire even if other_apex_*
    # has entries (they are irrelevant without a primary Apex account).
    res = check_compliance(
        _ok_ctx(
            eta_account_id=None,
            other_eta_account_ids=["apex-2"],
            has_opposing_apex_position=True,
        )
    )
    assert res.passed is True
    assert res.violations == []


# --------------------------------------------------------------------------- #
# NFA 2-29 promotional rule
# --------------------------------------------------------------------------- #


def test_promotional_without_disclaimer_blocks() -> None:
    res = check_compliance(
        _ok_ctx(
            is_promotional_communication=True,
            promotional_disclaimer_included=False,
        )
    )
    assert res.passed is False
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.NFA_2_29_PROMOTIONAL in rules


def test_promotional_with_disclaimer_passes() -> None:
    res = check_compliance(
        _ok_ctx(
            is_promotional_communication=True,
            promotional_disclaimer_included=True,
        )
    )
    assert res.passed is True


def test_non_promotional_skips_the_check() -> None:
    res = check_compliance(
        _ok_ctx(
            is_promotional_communication=False,
            promotional_disclaimer_included=False,
        )
    )
    assert res.passed is True


# --------------------------------------------------------------------------- #
# ComplianceCheckResult invariants
# --------------------------------------------------------------------------- #


def test_result_rejects_inconsistent_passed_with_blocking() -> None:
    # Constructing by hand with passed=True + BLOCKING violation must raise
    with pytest.raises(ValueError, match="inconsistent"):
        ComplianceCheckResult(
            passed=True,
            violations=[
                ComplianceViolation(
                    rule=ComplianceRuleId.NO_SELF_MATCH,
                    severity=Severity.BLOCKING,
                    message="x",
                ),
            ],
        )


def test_result_accepts_advisory_violations_with_passed_true() -> None:
    # Passed=True + ADVISORY-only violation is the expected normal case
    ComplianceCheckResult(
        passed=True,
        violations=[
            ComplianceViolation(
                rule=ComplianceRuleId.NO_LAYER_CANCEL,
                severity=Severity.ADVISORY,
                message="fast cancel",
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# Cross-rule interactions
# --------------------------------------------------------------------------- #


def test_multiple_blocking_rules_all_surface() -> None:
    res = check_compliance(
        _ok_ctx(
            operator_owned_account=False,
            external_capital_present=True,
            news_blackout_active=True,
        )
    )
    assert res.passed is False
    rules = [v.rule for v in res.violations]
    assert ComplianceRuleId.OWNS_ACCOUNT in rules
    assert ComplianceRuleId.NO_EXTERNAL_CAPITAL in rules
    assert ComplianceRuleId.APEX_NEWS_BLACKOUT in rules


def test_pretrade_context_rejects_zero_qty() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        PreTradeContext(
            account_id="x",
            symbol="MNQ",
            side="BUY",
            qty=0.0,
        )


def test_pretrade_context_rejects_unknown_side() -> None:
    with pytest.raises(ValueError):
        PreTradeContext(
            account_id="x",
            symbol="MNQ",
            side="FLOAT",  # type: ignore[arg-type]
            qty=1.0,
        )
