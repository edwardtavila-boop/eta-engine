"""
JARVIS v3 // prop_firm_guardrails — elite-level prop firm rule enforcement.

Every prop firm (BluSky, Apex, Topstep, Elite Trader Funding) imposes
hard rules that, if violated even once, blow the account permanently.
This module evaluates every new signal against the account's active
rule set BEFORE it becomes an order. If the trade — worst-case — would
breach any rule, the signal is rejected.

Why this lives in JARVIS, not in each individual bot
----------------------------------------------------

Bot-level rule enforcement scales O(bots × rules). Centralising the
gate means:

  * One source of truth for what counts as "max loss" per signal
  * One audit trail for every approval / rejection
  * Hermes can introspect "why was this signal denied?" via MCP
  * Operator can `/account` to see live headroom across all accounts
  * One place to add a new prop firm: drop a PropFirmRules into the registry

Rule shape
----------

The four canonical rules every futures prop firm has:

  * Daily loss limit ($) — hardest stop, intraday lockout
  * Trailing drawdown ($) — high-water-mark relative
  * Profit target ($) — eval phase only, evaluation pass threshold
  * Consistency rule (%) — funded phase, max % of total profit on any
    single day (typically 30%, prevents lottery-day phasing)

Optional rules:

  * Max contracts — hardware ceiling
  * RTH only — must close all positions before session close
  * Scaling plan — contracts ramp up as profit builds

Worst-case evaluation
---------------------

For each signal we conservatively assume the trade hits its full
stop-loss. ``max_loss_usd = abs(stop_r) * dollar_per_R * proposed_size``.
If that worst-case PnL would push the account past ANY rule's
threshold, the signal is denied.

Public interface
----------------

* ``REGISTRY`` — built-in rule profiles by ``(firm, size)`` key
* ``evaluate(rules, state, signal)`` → ``GuardrailVerdict``
* ``account_state_from_trades(account_id, trade_closes)`` → ``AccountState``
* ``aggregate_status(accounts)`` → list of ``AccountSnapshot``
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("eta_engine.brain.jarvis_v3.prop_firm_guardrails")

_WORKSPACE = Path(r"C:\EvolutionaryTradingAlgo")
_STATE_ROOT = _WORKSPACE / "var" / "eta_engine" / "state"
_LEGACY_STATE_ROOT = _WORKSPACE / "eta_engine" / "state"
_TRADE_CLOSES = _STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
_LEGACY_TRADE_CLOSES = _LEGACY_STATE_ROOT / "jarvis_intel" / "trade_closes.jsonl"
_ACCOUNT_MAP = _STATE_ROOT / "prop_firm_accounts.json"

EXPECTED_HOOKS = ("evaluate", "aggregate_status")

# Default $/R per contract for common futures (used when signal omits dollar_per_r)
_DEFAULT_DOLLAR_PER_R = {
    "MNQ": 20.0,  # Micro Nasdaq, 0.25 tick = $0.50, typical 40 ticks/R = $20
    "MES": 12.5,
    "MGC": 10.0,
    "MCL": 10.0,
    "M6E": 6.25,
    "NQ": 200.0,  # E-mini full size
    "ES": 125.0,
    "GC": 100.0,
    "CL": 100.0,
    "6E": 62.50,
}

# Verdict bands for headroom — the operator wants warnings, not just hard stops
_HEADROOM_WARN_PCT = 0.25  # < 25% of daily loss limit remaining → WARN
_HEADROOM_CRIT_PCT = 0.10  # < 10% of daily loss limit remaining → CRITICAL


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PropFirmRules:
    """One prop firm account's rule set."""

    firm: str  # "blusky" | "apex" | "topstep" | "etf"
    size: str  # "50K" | "100K" | "150K"
    account_id: str  # operator-assigned id (e.g. "blusky-launch-50k-001")
    starting_balance: float
    daily_loss_limit: float | None  # USD; hardest stop
    trailing_drawdown: float | None  # USD; high-water-mark relative
    profit_target: float | None  # USD; eval phase only
    consistency_rule_pct: float | None  # 0..1 (e.g. 0.30 means 30%)
    max_contracts: int | None
    rth_only: bool = False
    automation_allowed: bool = True  # see project_broker_routing for TOS matrix

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AccountState:
    """Current PnL state of one account, computed from trade_closes."""

    account_id: str
    starting_balance: float
    current_balance: float
    peak_balance: float  # high-water mark for trailing DD
    day_pnl_usd: float
    today_date: str  # YYYY-MM-DD UTC
    n_trades_today: int
    open_contracts: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AccountSnapshot:
    """Combined rules + state + computed headroom — what /account shows."""

    rules: PropFirmRules
    state: AccountState
    daily_loss_remaining: float | None
    daily_loss_pct_used: float | None  # 0..1
    trailing_dd_remaining: float | None
    profit_to_target: float | None
    pct_to_target: float | None
    severity: str  # "ok" | "warn" | "critical" | "blown"
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GuardrailVerdict:
    """Result of evaluating one proposed signal against the active rule set."""

    allowed: bool
    reason: str  # human-readable
    blockers: list[str]  # list of specific rule names that would fail
    headroom: dict[str, float]  # remaining capacity per rule
    worst_case_loss_usd: float
    asof: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Built-in rule registry — operator-tuneable defaults per the policy matrix
# ---------------------------------------------------------------------------

# Per project_broker_routing memory + project_prop_firm_bot_policy:
#   BluSky: allows full automation, $1500/day loss, $2000 trailing DD
#   Apex: TOS restricts automation on FUNDED accounts (eval ok)
#   Topstep: TOS restricts automation (we don't actively run there)
#   ETF: allows full automation
# Paper-test fleet baseline: the operator-facing "research portfolio" used
# for strategy soaks and crypto experimentation. NOT a prop firm — has no
# daily-loss/trailing-DD/profit-target rules — so bots routed here can
# stretch position sizes that would breach a prop-firm cap. Capital is
# notional, so the operator can iterate on strategy ideas without the
# 50K-prop-firm sizing ceiling artificially limiting performance.
#
try:
    _paper_test_cap = float(os.environ.get("ETA_PAPER_TEST_CAP_USD", "250000"))
    if _paper_test_cap < 50_000:
        _paper_test_cap = 50_000.0
except (TypeError, ValueError):
    _paper_test_cap = 250_000.0

PAPER_TEST_ACCOUNT_ID = "paper-test"
PAPER_TEST_CAP_USD = _paper_test_cap

REGISTRY: dict[str, PropFirmRules] = {
    PAPER_TEST_ACCOUNT_ID: PropFirmRules(
        firm="paper-test",
        size=f"{int(_paper_test_cap / 1000)}K",
        account_id=PAPER_TEST_ACCOUNT_ID,
        starting_balance=_paper_test_cap,
        # No breach rules — research portfolio is unconstrained so
        # strategies can show their real edge during paper soak.
        daily_loss_limit=None,
        trailing_drawdown=None,
        profit_target=None,
        consistency_rule_pct=None,
        max_contracts=None,
        rth_only=False,
        automation_allowed=True,
    ),
    "blusky-50K-launch": PropFirmRules(
        firm="blusky",
        size="50K",
        account_id="blusky-50K-launch",
        starting_balance=50_000.0,
        daily_loss_limit=1_500.0,
        trailing_drawdown=2_000.0,
        profit_target=3_000.0,
        consistency_rule_pct=None,  # Launch phase has no consistency rule
        max_contracts=10,
        rth_only=False,
        automation_allowed=True,
    ),
    "apex-50K-eval": PropFirmRules(
        firm="apex",
        size="50K",
        account_id="apex-50K-eval",
        starting_balance=50_000.0,
        daily_loss_limit=1_500.0,
        trailing_drawdown=2_500.0,
        profit_target=3_000.0,
        consistency_rule_pct=None,
        max_contracts=10,
        rth_only=False,
        automation_allowed=True,  # eval phase only
    ),
    "apex-50K-funded": PropFirmRules(
        firm="apex",
        size="50K",
        account_id="apex-50K-funded",
        starting_balance=50_000.0,
        daily_loss_limit=1_500.0,
        trailing_drawdown=2_500.0,
        profit_target=None,
        consistency_rule_pct=0.30,
        max_contracts=10,
        rth_only=False,
        automation_allowed=False,  # TOS restriction on funded accounts
    ),
    "topstep-50K": PropFirmRules(
        firm="topstep",
        size="50K",
        account_id="topstep-50K",
        starting_balance=50_000.0,
        daily_loss_limit=1_100.0,
        trailing_drawdown=2_000.0,
        profit_target=3_000.0,
        consistency_rule_pct=None,
        max_contracts=5,
        rth_only=True,
        automation_allowed=False,  # TOS restriction
    ),
    "etf-50K": PropFirmRules(
        firm="etf",
        size="50K",
        account_id="etf-50K",
        starting_balance=50_000.0,
        daily_loss_limit=1_500.0,
        trailing_drawdown=2_500.0,
        profit_target=3_000.0,
        consistency_rule_pct=None,
        max_contracts=10,
        rth_only=False,
        automation_allowed=True,
    ),
}


def list_known_accounts() -> list[str]:
    """Return all registered account IDs."""
    return sorted(REGISTRY.keys())


def get_rules(account_id: str) -> PropFirmRules | None:
    """Look up rules by account_id; returns None if not registered."""
    return REGISTRY.get(account_id)


# ---------------------------------------------------------------------------
# Account state computation
# ---------------------------------------------------------------------------


def _parse_iso(s: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_trades_for_account(
    account_id: str,
    trade_closes_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read all trades tagged for a given account_id.

    Wave-25 (2026-05-13): production reads (no override) go through
    closed_trade_ledger.load_close_records which filters to live+paper
    only. This is the prop-firm guardrail — we MUST NOT count
    backtest emissions toward the consistency / DD math for an
    actual prop account.

    Tests with explicit ``trade_closes_path`` keep the legacy
    single-source reader so they get exactly what they wrote.
    """
    if trade_closes_path is not None:
        paths = [trade_closes_path]
        out: list[dict[str, Any]] = []
        for path in paths:
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8") as fh:
                    for raw in fh:
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        rec_acct = str(rec.get("account_id") or "")
                        # paper-test catches untagged trades — see below.
                        if rec_acct == account_id or (account_id == PAPER_TEST_ACCOUNT_ID and rec_acct == ""):
                            out.append(rec)
            except OSError as exc:
                logger.warning("prop_firm_guardrails read failed (%s): %s", path, exc)
        return out

    from eta_engine.scripts.closed_trade_ledger import (
        DEFAULT_OPERATOR_DATA_SOURCES,
        load_close_records,
    )

    rows = load_close_records(
        source_paths=[_TRADE_CLOSES, _LEGACY_TRADE_CLOSES],
        data_sources=DEFAULT_OPERATOR_DATA_SOURCES,
    )
    # Routing rule:
    #   * Explicit tag wins: rec.account_id == account_id always matches.
    #   * Paper-test catch-all: when the caller asks for "paper-test", we
    #     also include records with NO account_id tag (the historical
    #     paper-fleet shape). This keeps the paper-test snapshot useful
    #     until every writer has been migrated to explicit tagging.
    #   * Real prop-firm accounts (blusky/apex/etc.) only match explicit
    #     tags — they NEVER inherit untagged paper trades, so the
    #     guardrails stay correct for the live cutover.
    return [
        r
        for r in rows
        if str(r.get("account_id") or "") == account_id
        or (
            account_id == PAPER_TEST_ACCOUNT_ID
            and str(r.get("account_id") or "") == ""
        )
    ]


def _trade_pnl_usd(rec: dict[str, Any]) -> float:
    """Best-effort dollar PnL from a trade close record.

    Priority order (post 2026-05-13 tick-leak fix):
      1. explicit USD field on the record (writer-supplied)
      2. ``extra.realized_pnl`` (writer-supplied USD inside the extra dict)
      3. sanitized R-value × $-per-R (last resort, after classify-drop
         of suspect tick-leak records)

    Critical for prop-firm drawdown enforcement: a single tick-leak
    record with ``realized_r=69`` on MNQ would otherwise compute as
    +$1,380 of phantom profit, swinging the trailing-drawdown high-
    water-mark and tripping false breach alerts.
    """
    # 1. explicit USD field wins
    for key in ("realized_pnl_usd", "pnl_usd", "realized_usd"):
        if key in rec:
            try:
                return float(rec[key])
            except (TypeError, ValueError):
                continue

    # 2. extra.realized_pnl wins next — this is the writer-supplied
    # dollar amount for the trade, available on post-fix closes.
    extra = rec.get("extra")
    if isinstance(extra, dict):
        pnl_raw = extra.get("realized_pnl")
        if pnl_raw is not None:
            try:
                return float(pnl_raw)
            except (TypeError, ValueError):
                pass

    # 3. Sanitized R × $-per-R fallback. classify() drops suspect
    # tick-leak rows (returns "suspect") so they contribute $0 rather
    # than blowing up the drawdown high-water-mark.
    from eta_engine.brain.jarvis_v3 import trade_close_sanitizer  # noqa: PLC0415

    status, value = trade_close_sanitizer.classify(rec)
    if status == "suspect" or status == "none" or value is None:
        return 0.0
    r = float(value)

    dollar_per_r = rec.get("dollar_per_r")
    if dollar_per_r is None:
        symbol = str(rec.get("symbol") or rec.get("instrument") or "").upper()
        # Strip any contract month suffix (MNQM6 → MNQ)
        for prefix in ("MNQ", "MES", "MGC", "MCL", "M6E", "NQ", "ES", "GC", "CL", "6E"):
            if symbol.startswith(prefix):
                dollar_per_r = _DEFAULT_DOLLAR_PER_R[prefix]
                break
    try:
        dollar_per_r = float(dollar_per_r) if dollar_per_r is not None else 0.0
    except (TypeError, ValueError):
        dollar_per_r = 0.0
    return r * dollar_per_r


def account_state_from_trades(
    account_id: str,
    *,
    trade_closes_path: Path | None = None,
    asof: datetime | None = None,
) -> AccountState:
    """Compute live account state from the trade_closes stream.

    Walks all trades for ``account_id``, builds the equity curve, and
    extracts peak balance + today's PnL. Open positions are read from
    a separate flag (trades flagged ``still_open: True`` are not yet
    closed). The returned state is the basis for every guardrail check.
    """
    rules = REGISTRY.get(account_id)
    starting = rules.starting_balance if rules else 0.0

    asof_dt = asof or datetime.now(UTC)
    today_iso = asof_dt.date().isoformat()

    trades = _read_trades_for_account(account_id, trade_closes_path)
    current_balance = starting
    peak_balance = starting
    day_pnl = 0.0
    n_trades_today = 0

    for rec in trades:
        pnl = _trade_pnl_usd(rec)
        current_balance += pnl
        if current_balance > peak_balance:
            peak_balance = current_balance
        ts = _parse_iso(rec.get("ts") or rec.get("closed_at"))
        if ts is not None and ts.date().isoformat() == today_iso:
            day_pnl += pnl
            n_trades_today += 1

    return AccountState(
        account_id=account_id,
        starting_balance=starting,
        current_balance=round(current_balance, 2),
        peak_balance=round(peak_balance, 2),
        day_pnl_usd=round(day_pnl, 2),
        today_date=today_iso,
        n_trades_today=n_trades_today,
        open_contracts=0,  # filled by live broker reader; 0 here
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate(
    rules: PropFirmRules,
    state: AccountState,
    signal: dict[str, Any],
) -> GuardrailVerdict:
    """Worst-case evaluation of one proposed signal against active rules.

    The signal dict must include:
      * ``symbol`` — e.g. ``"MNQ"``
      * ``stop_r`` — stop distance in R (typically 1.0 for a 1R stop)
      * ``size`` — number of contracts
      * (optional) ``dollar_per_r`` — override the default $/R for this symbol

    The worst-case loss is ``|stop_r| * dollar_per_r * size``. If that
    loss would push any rule past its threshold, the signal is denied.

    NEVER raises. On any input weirdness, returns a default-deny verdict
    with the failure recorded — fail-closed is the right posture for a
    money-handling gate.
    """
    asof_iso = datetime.now(UTC).isoformat()
    blockers: list[str] = []

    # Hard reject: automation disallowed on this account (TOS)
    if not rules.automation_allowed:
        return GuardrailVerdict(
            allowed=False,
            reason=f"automation_disallowed on {rules.firm} {rules.size}",
            blockers=["tos_automation"],
            headroom={},
            worst_case_loss_usd=0.0,
            asof=asof_iso,
        )

    # Read signal — fail closed on any malformed input.
    # NOTE: use explicit None checks, not truthy fallbacks: ``0 or 1 == 1``
    # would silently rescue a malformed zero-size signal.
    try:
        symbol = str(signal.get("symbol") or "").upper()
        raw_stop = signal.get("stop_r")
        stop_r = abs(float(raw_stop if raw_stop is not None else 1.0))
        raw_size = signal.get("size")
        size = int(raw_size if raw_size is not None else 1)
    except (TypeError, ValueError) as exc:
        return GuardrailVerdict(
            allowed=False,
            reason=f"malformed signal: {exc}",
            blockers=["malformed_signal"],
            headroom={},
            worst_case_loss_usd=0.0,
            asof=asof_iso,
        )

    if size <= 0:
        return GuardrailVerdict(
            allowed=False,
            reason="size must be positive",
            blockers=["malformed_signal"],
            headroom={},
            worst_case_loss_usd=0.0,
            asof=asof_iso,
        )

    # Determine $/R
    dollar_per_r = signal.get("dollar_per_r")
    if dollar_per_r is None:
        for prefix, default_r in _DEFAULT_DOLLAR_PER_R.items():
            if symbol.startswith(prefix):
                dollar_per_r = default_r
                break
    try:
        dollar_per_r = float(dollar_per_r) if dollar_per_r is not None else 0.0
    except (TypeError, ValueError):
        dollar_per_r = 0.0
    if dollar_per_r <= 0:
        return GuardrailVerdict(
            allowed=False,
            reason=f"unknown symbol $/R: {symbol}",
            blockers=["unknown_symbol"],
            headroom={},
            worst_case_loss_usd=0.0,
            asof=asof_iso,
        )

    worst_case_loss = stop_r * dollar_per_r * size

    headroom: dict[str, float] = {}

    # Rule 1: max contracts
    if rules.max_contracts is not None and size > rules.max_contracts:
        blockers.append(f"max_contracts({size}>{rules.max_contracts})")
    headroom["max_contracts_remaining"] = float(rules.max_contracts - size) if rules.max_contracts else float("inf")

    # Rule 2: daily loss limit
    if rules.daily_loss_limit is not None:
        # Already at limit? hard reject
        # Note: state.day_pnl_usd is negative when losing
        day_loss = -min(0.0, state.day_pnl_usd)  # convert to positive loss
        projected_loss = day_loss + worst_case_loss
        remaining = max(0.0, rules.daily_loss_limit - day_loss)
        headroom["daily_loss_remaining_usd"] = round(remaining, 2)
        if projected_loss >= rules.daily_loss_limit:
            blockers.append(f"daily_loss({projected_loss:.0f}>={rules.daily_loss_limit:.0f})")

    # Rule 3: trailing drawdown
    if rules.trailing_drawdown is not None:
        dd_now = state.peak_balance - state.current_balance
        projected_dd = dd_now + worst_case_loss
        remaining_dd = max(0.0, rules.trailing_drawdown - dd_now)
        headroom["trailing_dd_remaining_usd"] = round(remaining_dd, 2)
        if projected_dd >= rules.trailing_drawdown:
            blockers.append(f"trailing_dd({projected_dd:.0f}>={rules.trailing_drawdown:.0f})")

    # Rule 4: consistency rule (funded phase only — applies to PROFIT distribution)
    # Only enforced when we're at the profit target threshold or beyond;
    # before then it's an informational headroom, not a blocker.
    if rules.consistency_rule_pct is not None and state.day_pnl_usd > 0:
        total_profit = state.current_balance - state.starting_balance
        if total_profit > 0:
            today_pct = state.day_pnl_usd / total_profit
            headroom["consistency_pct_used"] = round(today_pct, 4)
            if today_pct > rules.consistency_rule_pct:
                blockers.append(f"consistency({today_pct:.0%}>{rules.consistency_rule_pct:.0%})")

    if blockers:
        return GuardrailVerdict(
            allowed=False,
            reason=f"would breach: {', '.join(blockers)}",
            blockers=blockers,
            headroom=headroom,
            worst_case_loss_usd=round(worst_case_loss, 2),
            asof=asof_iso,
        )

    return GuardrailVerdict(
        allowed=True,
        reason="all rules pass",
        blockers=[],
        headroom=headroom,
        worst_case_loss_usd=round(worst_case_loss, 2),
        asof=asof_iso,
    )


# ---------------------------------------------------------------------------
# Aggregate / dashboard
# ---------------------------------------------------------------------------


def _severity_for(rules: PropFirmRules, state: AccountState) -> tuple[str, list[str]]:
    """Compute the severity tag + list of triggered limits for one account."""
    blockers: list[str] = []
    severity = "ok"
    if rules.daily_loss_limit is not None:
        day_loss = -min(0.0, state.day_pnl_usd)
        if day_loss >= rules.daily_loss_limit:
            blockers.append("daily_loss_blown")
            return "blown", blockers
        used_pct = day_loss / rules.daily_loss_limit
        if used_pct >= 1 - _HEADROOM_CRIT_PCT:
            severity = "critical"
            blockers.append(f"daily_loss_{used_pct:.0%}")
        elif used_pct >= 1 - _HEADROOM_WARN_PCT:
            if severity == "ok":
                severity = "warn"
    if rules.trailing_drawdown is not None:
        dd = state.peak_balance - state.current_balance
        if dd >= rules.trailing_drawdown:
            blockers.append("trailing_dd_blown")
            return "blown", blockers
        used_pct = dd / rules.trailing_drawdown
        if used_pct >= 1 - _HEADROOM_CRIT_PCT:
            severity = "critical"
            blockers.append(f"trailing_dd_{used_pct:.0%}")
        elif used_pct >= 1 - _HEADROOM_WARN_PCT and severity == "ok":
            severity = "warn"
    return severity, blockers


def snapshot_one(
    account_id: str,
    *,
    trade_closes_path: Path | None = None,
    asof: datetime | None = None,
) -> AccountSnapshot | None:
    """Build the full snapshot for one account, or None if unregistered."""
    rules = REGISTRY.get(account_id)
    if rules is None:
        return None
    state = account_state_from_trades(account_id, trade_closes_path=trade_closes_path, asof=asof)
    severity, blockers = _severity_for(rules, state)

    daily_loss_remaining: float | None = None
    daily_loss_pct_used: float | None = None
    if rules.daily_loss_limit is not None:
        day_loss = -min(0.0, state.day_pnl_usd)
        daily_loss_remaining = round(max(0.0, rules.daily_loss_limit - day_loss), 2)
        daily_loss_pct_used = round(day_loss / rules.daily_loss_limit, 4)

    trailing_dd_remaining: float | None = None
    if rules.trailing_drawdown is not None:
        dd = state.peak_balance - state.current_balance
        trailing_dd_remaining = round(max(0.0, rules.trailing_drawdown - dd), 2)

    profit_to_target: float | None = None
    pct_to_target: float | None = None
    if rules.profit_target is not None:
        profit = state.current_balance - state.starting_balance
        profit_to_target = round(rules.profit_target - profit, 2)
        pct_to_target = round(profit / rules.profit_target, 4)

    return AccountSnapshot(
        rules=rules,
        state=state,
        daily_loss_remaining=daily_loss_remaining,
        daily_loss_pct_used=daily_loss_pct_used,
        trailing_dd_remaining=trailing_dd_remaining,
        profit_to_target=profit_to_target,
        pct_to_target=pct_to_target,
        severity=severity,
        blockers=blockers,
    )


def aggregate_status(
    *,
    trade_closes_path: Path | None = None,
    asof: datetime | None = None,
) -> list[AccountSnapshot]:
    """Return snapshots for every registered account, sorted by severity desc."""
    sev_order = {"blown": 0, "critical": 1, "warn": 2, "ok": 3}
    snaps: list[AccountSnapshot] = []
    for account_id in REGISTRY:
        snap = snapshot_one(account_id, trade_closes_path=trade_closes_path, asof=asof)
        if snap is not None:
            snaps.append(snap)
    snaps.sort(key=lambda s: (sev_order.get(s.severity, 9), s.rules.account_id))
    return snaps
