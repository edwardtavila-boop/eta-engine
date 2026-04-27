"""
EVOLUTIONARY TRADING ALGO  //  core.cftc_nfa_compliance
===========================================
CFTC / NFA compliance pre-trade checklist for retail futures trading.

Scope
-----
This module is NOT legal advice. It encodes the retail-futures guardrails
that apply to us specifically:

  * Operator = non-registered individual trading own capital.
  * Venues   = IBKR + Tastytrade (active, both NFA member brokers on
               CFTC-regulated exchanges). Tradovate is also an NFA
               member broker but is DORMANT per operator mandate
               2026-04-24 (funding-blocked).
  * Product  = MNQ / NQ (CME) -- Section 1256 60/40 tax treatment.
  * Scale    = personal SSN, no LLC, no pooled capital.
  * Pattern  = intraday + overnight; no customer money.

What we actually need to enforce here is narrow: we are not a CPO, not a
CTA, and not a FCM. But we DO need to avoid the patterns that trip
registration thresholds or NFA disciplinary rules:

  1. "Trading for others" -- if the strategy ever executes under another
     person's account it becomes a CTA activity. Enforced: every order
     must carry an operator-owned account id.
  2. "Touting results" -- NFA Rule 2-29 restricts performance reps. We
     mark every artifact as internal and never expose externally from
     code paths the operator didn't explicitly allow.
  3. "Pool management" -- CPO threshold kicks in if we accept outside
     capital. Enforced: config-level deposit whitelist must be empty for
     anyone other than the operator.
  4. "Market manipulation" -- spoofing / layering / wash trading are
     flat-out illegal. Enforced: pre-trade checks for self-match, for
     the same-account-both-sides pattern, and for rapid cancel-replace
     velocity (> 1 Hz per order id -> flagged).
  5. "Eval account abuse" -- Apex Trader Funding rules (non-government
     but contractual): no hedging another Apex account, no news trading
     during blackout, no copy-trading between accounts.

On a violation: ``ComplianceCheckResult.pass_ = False`` and the offending
rule id is returned. The caller (typically ``core.risk_engine`` or a
pre-order gate) must abort the order and log the kill-log entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Rule ids / types
# ---------------------------------------------------------------------------


class ComplianceRuleId(StrEnum):
    """Stable machine-readable rule ids we can cite in kill-log entries."""

    OWNS_ACCOUNT = "CFTC.OWNS_ACCOUNT"
    NO_EXTERNAL_CAPITAL = "CFTC.NO_EXTERNAL_CAPITAL"
    NO_POOL_MANAGEMENT = "CFTC.NO_POOL_MANAGEMENT"
    NO_SELF_MATCH = "CFTC.NO_SELF_MATCH"
    NO_LAYER_CANCEL = "CFTC.NO_LAYER_CANCEL"
    APEX_ONE_ACCOUNT = "APEX.ONE_ACCOUNT_PER_TRADE"
    APEX_NO_CROSS_HEDGE = "APEX.NO_CROSS_HEDGE"
    APEX_NEWS_BLACKOUT = "APEX.NEWS_BLACKOUT"
    NFA_2_29_PROMOTIONAL = "NFA.2_29_PROMOTIONAL"


class Severity(StrEnum):
    """How loud the failure is."""

    BLOCKING = "BLOCKING"  # refuse the order
    ADVISORY = "ADVISORY"  # allow but log


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PreTradeContext(BaseModel):
    """Snapshot of everything the compliance engine needs for one order."""

    operator_owned_account: bool = True
    account_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Literal["BUY", "SELL"]
    qty: float = Field(gt=0.0)
    external_capital_present: bool = False
    deposit_whitelist: list[str] = Field(default_factory=list)
    # Apex eval context
    eta_account_id: str | None = None
    other_eta_account_ids: list[str] = Field(default_factory=list)
    has_opposing_apex_position: bool = False
    # Self-match / layering
    open_order_ids_same_symbol: list[str] = Field(default_factory=list)
    order_cancel_rate_hz: float = 0.0
    bid_ask_self_overlap: bool = False
    # News blackout
    news_blackout_active: bool = False
    # NFA-2-29
    is_promotional_communication: bool = False
    promotional_disclaimer_included: bool = False


class ComplianceViolation(BaseModel):
    """A single broken rule."""

    rule: ComplianceRuleId
    severity: Severity
    message: str


class ComplianceCheckResult(BaseModel):
    """Aggregate of all checks against one context."""

    passed: bool
    violations: list[ComplianceViolation] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _ensure_passed_matches_violations(self) -> ComplianceCheckResult:
        blocking = any(v.severity == Severity.BLOCKING for v in self.violations)
        if blocking and self.passed:
            raise ValueError(
                "passed=True is inconsistent with BLOCKING violations",
            )
        return self


# ---------------------------------------------------------------------------
# Individual rule checks
# ---------------------------------------------------------------------------


def _check_owns_account(ctx: PreTradeContext) -> ComplianceViolation | None:
    if not ctx.operator_owned_account:
        return ComplianceViolation(
            rule=ComplianceRuleId.OWNS_ACCOUNT,
            severity=Severity.BLOCKING,
            message=(
                "Order targets an account the operator does not own. This "
                "would constitute trading-for-others and could trigger CTA "
                "registration requirements."
            ),
        )
    return None


def _check_no_external_capital(ctx: PreTradeContext) -> ComplianceViolation | None:
    if ctx.external_capital_present:
        return ComplianceViolation(
            rule=ComplianceRuleId.NO_EXTERNAL_CAPITAL,
            severity=Severity.BLOCKING,
            message=(
                "External capital detected. Pooled-fund trading triggers CPO "
                "registration -- outside our current compliance posture."
            ),
        )
    return None


def _check_no_pool(ctx: PreTradeContext) -> ComplianceViolation | None:
    # Whitelist must contain only the operator. Any other entry means
    # the config is accepting deposits from someone else.
    non_operator = [x for x in ctx.deposit_whitelist if x and x != ctx.account_id]
    if non_operator:
        return ComplianceViolation(
            rule=ComplianceRuleId.NO_POOL_MANAGEMENT,
            severity=Severity.BLOCKING,
            message=(
                f"deposit_whitelist contains non-operator addresses: {non_operator}. "
                "Zero out config.allowed_depositors."
            ),
        )
    return None


def _check_no_self_match(ctx: PreTradeContext) -> ComplianceViolation | None:
    if ctx.bid_ask_self_overlap:
        return ComplianceViolation(
            rule=ComplianceRuleId.NO_SELF_MATCH,
            severity=Severity.BLOCKING,
            message=(
                "Self-match detected: proposed order would cross our own "
                "open resting order on the other side. Cancel the resting "
                "leg before submitting."
            ),
        )
    return None


def _check_no_layer_cancel(ctx: PreTradeContext) -> ComplianceViolation | None:
    if ctx.order_cancel_rate_hz > 1.0:
        return ComplianceViolation(
            rule=ComplianceRuleId.NO_LAYER_CANCEL,
            severity=Severity.ADVISORY,
            message=(
                f"order cancel rate {ctx.order_cancel_rate_hz:.2f} Hz exceeds "
                "1 Hz per id -- potential layering pattern. Slow the sniper."
            ),
        )
    return None


def _check_apex_one_account(ctx: PreTradeContext) -> ComplianceViolation | None:
    # An Apex eval rule: do not copy-trade across accounts. We approximate
    # this by requiring that eta_account_id, when present, is the only
    # Apex account currently engaged in the same symbol.
    if ctx.eta_account_id is not None and len(ctx.other_eta_account_ids) > 0:
        return ComplianceViolation(
            rule=ComplianceRuleId.APEX_ONE_ACCOUNT,
            severity=Severity.ADVISORY,
            message=(
                "Multiple Apex accounts engaged simultaneously: "
                f"{[ctx.eta_account_id, *ctx.other_eta_account_ids]}. "
                "Apex rules prohibit copy-trading between accounts."
            ),
        )
    return None


def _check_apex_no_cross_hedge(ctx: PreTradeContext) -> ComplianceViolation | None:
    if ctx.eta_account_id is not None and ctx.has_opposing_apex_position:
        return ComplianceViolation(
            rule=ComplianceRuleId.APEX_NO_CROSS_HEDGE,
            severity=Severity.BLOCKING,
            message=(
                "Opposing Apex position detected on a related account. "
                "Cross-account hedging violates Apex rules; reduce or flat "
                "the other side first."
            ),
        )
    return None


def _check_apex_news_blackout(ctx: PreTradeContext) -> ComplianceViolation | None:
    if ctx.news_blackout_active:
        return ComplianceViolation(
            rule=ComplianceRuleId.APEX_NEWS_BLACKOUT,
            severity=Severity.BLOCKING,
            message=(
                "Apex news-blackout window is active (CPI/FOMC/NFP etc.). "
                "Trading during blackout can void the eval; order rejected."
            ),
        )
    return None


def _check_nfa_2_29(ctx: PreTradeContext) -> ComplianceViolation | None:
    if ctx.is_promotional_communication and not ctx.promotional_disclaimer_included:
        return ComplianceViolation(
            rule=ComplianceRuleId.NFA_2_29_PROMOTIONAL,
            severity=Severity.BLOCKING,
            message=(
                "Promotional communication without NFA 2-29 disclaimers "
                "(hypothetical-performance / past-performance / no-guarantee)."
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Aggregate check
# ---------------------------------------------------------------------------

_CHECKS = (
    _check_owns_account,
    _check_no_external_capital,
    _check_no_pool,
    _check_no_self_match,
    _check_no_layer_cancel,
    _check_apex_one_account,
    _check_apex_no_cross_hedge,
    _check_apex_news_blackout,
    _check_nfa_2_29,
)


def check_compliance(ctx: PreTradeContext) -> ComplianceCheckResult:
    """Run every rule and return an aggregate result.

    Passes = no BLOCKING violations. ADVISORY failures still show up in
    the violations list but do not flip ``passed`` to False.
    """
    violations: list[ComplianceViolation] = []
    for check in _CHECKS:
        v = check(ctx)
        if v is not None:
            violations.append(v)
    has_block = any(v.severity == Severity.BLOCKING for v in violations)
    return ComplianceCheckResult(passed=not has_block, violations=violations)
