"""
EVOLUTIONARY TRADING ALGO  //  brain.jarvis_admin
=====================================
JARVIS as admin of all projects.

Why this exists
---------------
Edward's directive: "make him [Jarvis] the admin of all my projects -- make
everyone report to him throughout my framework."

Before v2, Jarvis was a passive observer -- producing a JarvisContext on
demand. Every autonomous subsystem (bots, firm agents, autopilot, gates,
orchestrators) made its own decisions and possibly *later* looked at
Jarvis's suggestion.

With JarvisAdmin, the flow inverts: every subsystem MUST request approval
from Jarvis before taking an autonomous action. Jarvis evaluates the
request against its live context (stress score, session phase, action
tier, alerts, margins) and returns APPROVED / DENIED / CONDITIONAL /
DEFERRED with a machine-readable reason code and optional size cap.

Design principles
-----------------
1. Non-breaking / opt-in. Subsystems that don't call JarvisAdmin continue
   to work. Adoption is incremental. Every call goes through the same
   ``request_approval()`` entrypoint.
2. Every request+response logged to an append-only JSONL -- the chain of
   command is fully auditable.
3. Pydantic-typed request / response models. No untyped dicts.
4. Policy engine is pure and deterministic given a JarvisContext +
   ActionRequest. No network, no I/O.
5. The only side effect is the audit log append (if a path is
   provided).
6. Kill-switch honored first. Nothing gets past a KILL verdict except
   operator-triggered reset.

Public API
----------
  * ``SubsystemId``         -- enum of every subsystem in the fleet
  * ``ActionType``          -- enum of every action kind a subsystem can request
  * ``Verdict``             -- APPROVED / DENIED / CONDITIONAL / DEFERRED
  * ``ActionRequest``       -- pydantic: subsystem + action + payload + rationale
  * ``ActionResponse``      -- pydantic: verdict + reason + conditions + context
                               snapshot summary
  * ``JarvisAdmin``         -- the authority class itself
  * ``evaluate_request``    -- pure policy function (exposed for testing)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime  # noqa: TC003  -- pydantic needs runtime
from enum import StrEnum
from pathlib import Path  # noqa: TC003  -- pydantic needs runtime for audit_path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eta_engine.brain.jarvis_context import (
    ActionSuggestion,
    JarvisContext,
    JarvisContextEngine,
    SessionPhase,
)
from eta_engine.brain.model_policy import (
    ModelTier,
    TaskCategory,
    select_model,
)
from eta_engine.core.market_quality import (
    build_market_context_summary,
    format_market_context_summary,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subsystem registry -- every autonomous actor in the fleet
# ---------------------------------------------------------------------------


class SubsystemId(StrEnum):
    """Every autonomous action-taking component that must report to Jarvis.

    Keep these stable -- they end up as labels in the audit log.
    """

    # eta_engine bot fleet (portfolio)
    # L1 / equity index
    BOT_MNQ = "bot.mnq"
    BOT_NQ = "bot.nq"
    # L2 / BTC hybrid (grid-in-range + directional-on-trend)
    BOT_BTC_HYBRID = "bot.btc_hybrid"
    # L3 / multi-perp alpha desk  (BTC, ETH, SOL, XRP)
    BOT_BTC_PERP = "bot.btc_perp"
    BOT_ETH_PERP = "bot.eth_perp"
    BOT_SOL_PERP = "bot.sol_perp"
    BOT_XRP_PERP = "bot.xrp_perp"
    # L4 / yield infrastructure -- aggregator that submits per-protocol
    # PROTOCOL_EXPOSURE requests; specific protocol disambiguated in payload.
    BOT_YIELD_VAULT = "bot.yield_vault"
    # legacy / seed-capital misc crypto bot
    BOT_CRYPTO_SEED = "bot.crypto_seed"

    # mnq_bot v3 framework
    FRAMEWORK_AUTOPILOT = "framework.autopilot"
    FRAMEWORK_FIRM_ENGINE = "framework.firm_engine"
    FRAMEWORK_COURT_OF_APPEALS = "framework.court_of_appeals"
    FRAMEWORK_CONFLUENCE = "framework.confluence_scorer"
    FRAMEWORK_WEBHOOK = "framework.webhook"
    FRAMEWORK_META_ORCH = "framework.meta_orchestrator"

    # the_firm 6-agent adversarial system
    AGENT_QUANT = "firm.quant"
    AGENT_RED_TEAM = "firm.red_team"
    AGENT_RISK = "firm.risk"
    AGENT_MACRO = "firm.macro"
    AGENT_MICRO = "firm.micro"
    AGENT_PM = "firm.pm"

    # gate + watchdog + telemetry
    GATE_CHAIN = "gates.chain"
    AUTOPILOT_WATCHDOG = "watchdog.autopilot"

    # operator (still must report when exercising override authority)
    OPERATOR = "operator.edward"


# Convenience: bots that trade 24/7 crypto markets and thus need the
# overnight whitelist. Grouping them here so policy code stays readable.
CRYPTO_24_7_BOTS: frozenset[SubsystemId] = frozenset(
    {
        SubsystemId.BOT_CRYPTO_SEED,
        SubsystemId.BOT_BTC_HYBRID,
        SubsystemId.BOT_BTC_PERP,
        SubsystemId.BOT_ETH_PERP,
        SubsystemId.BOT_SOL_PERP,
        SubsystemId.BOT_XRP_PERP,
        SubsystemId.BOT_YIELD_VAULT,
    }
)


# ---------------------------------------------------------------------------
# Action taxonomy
# ---------------------------------------------------------------------------


class ActionType(StrEnum):
    """Kind of autonomous action a subsystem is requesting approval for."""

    # signal / decision lifecycle
    SIGNAL_EMIT = "SIGNAL_EMIT"  # strategy produces a signal
    # order lifecycle
    ORDER_PLACE = "ORDER_PLACE"
    ORDER_MODIFY = "ORDER_MODIFY"  # move stop, change size, etc.
    ORDER_CANCEL = "ORDER_CANCEL"
    POSITION_FLATTEN = "POSITION_FLATTEN"  # emergency exit one position
    # system-level
    KILL_SWITCH_TRIP = "KILL_SWITCH_TRIP"
    KILL_SWITCH_RESET = "KILL_SWITCH_RESET"  # operator only
    AUTOPILOT_RESUME = "AUTOPILOT_RESUME"
    GATE_OVERRIDE = "GATE_OVERRIDE"  # operator overriding a blocking gate
    # strategy / portfolio lifecycle
    STRATEGY_DEPLOY = "STRATEGY_DEPLOY"  # promote paper -> live
    STRATEGY_RETIRE = "STRATEGY_RETIRE"
    PARAMETER_CHANGE = "PARAMETER_CHANGE"  # tune size/stop/target
    CAPITAL_ALLOCATE = "CAPITAL_ALLOCATE"  # move capital between bots
    # L4 / yield-infrastructure actions
    PROTOCOL_EXPOSURE = "PROTOCOL_EXPOSURE"  # open/increase a DeFi position
    REBALANCE = "REBALANCE"  # periodic ledger reconciliation
    # LLM routing (not a trading action -- a cost-optimization decision)
    LLM_INVOCATION = "LLM_INVOCATION"  # which model tier for this task?


class Verdict(StrEnum):
    APPROVED = "APPROVED"
    CONDITIONAL = "CONDITIONAL"  # approved WITH conditions (size cap, etc.)
    DENIED = "DENIED"
    DEFERRED = "DEFERRED"  # try again later (e.g. wait for macro event)


# ---------------------------------------------------------------------------
# Request / response envelopes
# ---------------------------------------------------------------------------


def _new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class ActionRequest(BaseModel):
    """A subsystem asking Jarvis for permission to take an action."""

    model_config = ConfigDict(frozen=False)  # allow request_id default factory

    request_id: str = Field(default_factory=_new_request_id, min_length=1)
    subsystem: SubsystemId
    action: ActionType
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Action-specific fields, e.g. {'side':'long', 'qty':2}",
    )
    rationale: str = Field(
        default="",
        description="One-sentence explanation of why the subsystem wants this action. Logged for post-hoc review.",
    )
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ActionResponse(BaseModel):
    """Jarvis's verdict on an ActionRequest."""

    request_id: str = Field(min_length=1)
    verdict: Verdict
    reason: str = Field(min_length=1)
    reason_code: str = Field(
        min_length=1,
        description="Stable machine-readable code, e.g. 'dd_over_kill'.",
    )
    conditions: list[str] = Field(default_factory=list)
    # live-context snapshot (denormalized for audit convenience)
    jarvis_action: ActionSuggestion
    stress_composite: float = Field(ge=0.0, le=1.0)
    session_phase: SessionPhase
    binding_constraint: str = ""
    size_cap_mult: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Max size multiplier approved (None = no explicit cap).",
    )
    # LLM routing decision (populated only for ActionType.LLM_INVOCATION).
    # Backward-compatible: existing trading-action callers will see None.
    selected_model: ModelTier | None = Field(
        default=None,
        description="Model tier chosen by model_policy for this task. Only set when action == LLM_INVOCATION.",
    )
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


# Actions that are ALWAYS permitted (protective / exit-only).
_EXIT_ONLY_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.ORDER_CANCEL,
        ActionType.POSITION_FLATTEN,
        ActionType.KILL_SWITCH_TRIP,
    }
)

# Actions that grow risk (net-new exposure).
_RISK_ADDING_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.SIGNAL_EMIT,
        ActionType.ORDER_PLACE,
        ActionType.STRATEGY_DEPLOY,
        ActionType.CAPITAL_ALLOCATE,
        # L4 position opens grow principal-loss exposure (slashing, depeg,
        # smart-contract) so they gate like trading entries.
        ActionType.PROTOCOL_EXPOSURE,
    }
)

# Operator-only -- no bot may trigger these.
_OPERATOR_ONLY: frozenset[ActionType] = frozenset(
    {
        ActionType.KILL_SWITCH_RESET,
        ActionType.GATE_OVERRIDE,
        ActionType.AUTOPILOT_RESUME,
    }
)


def evaluate_llm_request(req: ActionRequest) -> ActionResponse:
    """Pure policy for LLM tier selection -- no JarvisContext needed.

    Reads ``req.payload['task_category']`` (a ``TaskCategory`` value) and
    returns an ActionResponse with ``selected_model`` set to the tier
    ``model_policy.select_model`` picks.

    Behavior:
      * Missing category    -> DEFERRED, reason_code='llm_missing_category'.
      * Unknown category    -> CONDITIONAL, defaults to SONNET.
      * Known category      -> APPROVED, model tier per policy.

    We fabricate a minimal ActionResponse without the live-context fields
    (stress, phase, binding_constraint) because model routing is
    stress-independent. Existing fields keep sentinel defaults.
    """
    raw_category = req.payload.get("task_category")
    if raw_category is None:
        return ActionResponse(
            request_id=req.request_id,
            verdict=Verdict.DEFERRED,
            reason="LLM_INVOCATION requires payload['task_category']",
            reason_code="llm_missing_category",
            jarvis_action=ActionSuggestion.TRADE,  # no live context; trivial
            stress_composite=0.0,
            session_phase=SessionPhase.OVERNIGHT,
        )
    try:
        category = TaskCategory(raw_category)
    except ValueError:
        fallback = select_model(TaskCategory.STRATEGY_EDIT)
        return ActionResponse(
            request_id=req.request_id,
            verdict=Verdict.CONDITIONAL,
            reason=(f"unknown task_category={raw_category!r}; defaulting to {fallback.tier.value}"),
            reason_code="llm_unknown_category_default",
            conditions=[f"model={fallback.tier.value}"],
            jarvis_action=ActionSuggestion.TRADE,
            stress_composite=0.0,
            session_phase=SessionPhase.OVERNIGHT,
            selected_model=fallback.tier,
        )
    sel = select_model(category)
    return ActionResponse(
        request_id=req.request_id,
        verdict=Verdict.APPROVED,
        reason=sel.reason,
        reason_code=f"llm_{sel.tier.value}",
        conditions=[
            f"model={sel.tier.value}",
            f"bucket={sel.bucket.value}",
            f"cost_multiplier={sel.cost_multiplier:.2f}",
        ],
        jarvis_action=ActionSuggestion.TRADE,
        stress_composite=0.0,
        session_phase=SessionPhase.OVERNIGHT,
        selected_model=sel.tier,
    )


def evaluate_request(
    req: ActionRequest,
    ctx: JarvisContext,
) -> ActionResponse:
    """Pure policy from a request + current Jarvis context.

    Ordering -- first match wins:
      1. Operator-only actions refused when subsystem != OPERATOR.
      2. KILL action tier refuses everything except KILL_SWITCH_RESET
         from operator and exit-only from bots.
      3. KILL_SWITCH_TRIP always allowed (must work in every mode).
      4. STAND_ASIDE refuses risk-adding actions; exit-only permitted.
      5. REDUCE allows risk-adding but CONDITIONAL with a size cap
         from the live sizing hint (capped at 0.50).
      6. REVIEW actions require ``payload['review_acknowledged']=True``.
      7. Session gates:
         * OVERNIGHT refuses risk-adding unless subsystem is in the
           overnight whitelist and payload['overnight_explicit']=True.
         * CLOSE refuses risk-adding entries in last 15 minutes.
      8. TRADE tier -> APPROVED (with size_cap from sizing_hint).
    """

    def _build(
        verdict: Verdict,
        reason: str,
        reason_code: str,
        *,
        conditions: list[str] | None = None,
        size_cap_mult: float | None = None,
    ) -> ActionResponse:
        return ActionResponse(
            request_id=req.request_id,
            verdict=verdict,
            reason=reason,
            reason_code=reason_code,
            conditions=conditions or [],
            jarvis_action=ctx.suggestion.action,
            stress_composite=(ctx.stress_score.composite if ctx.stress_score else 0.0),
            session_phase=(ctx.session_phase or SessionPhase.OVERNIGHT),
            binding_constraint=(ctx.stress_score.binding_constraint if ctx.stress_score else ""),
            size_cap_mult=size_cap_mult,
        )

    # 0. LLM invocation -- cost-optimization decision, not a risk gate.
    # Short-circuits ahead of every other rule because model routing is
    # stress-independent; see ``evaluate_llm_request`` for the policy.
    if req.action == ActionType.LLM_INVOCATION:
        return evaluate_llm_request(req)

    live_size = ctx.sizing_hint.size_mult if ctx.sizing_hint is not None else 1.0

    # 1. Operator-only actions -- non-operators are refused.
    if req.action in _OPERATOR_ONLY and req.subsystem != SubsystemId.OPERATOR:
        return _build(
            Verdict.DENIED,
            f"{req.action.value} is operator-only; {req.subsystem.value} not authorized",
            reason_code="operator_only_action",
        )

    # 2. KILL tier -- allow only exit-only actions and operator reset.
    if ctx.suggestion.action == ActionSuggestion.KILL:
        if req.action == ActionType.KILL_SWITCH_RESET:
            # operator may reset (guarded by _OPERATOR_ONLY above)
            return _build(
                Verdict.APPROVED,
                "operator resetting kill switch",
                reason_code="operator_reset",
            )
        if req.action in _EXIT_ONLY_ACTIONS:
            return _build(
                Verdict.APPROVED,
                "exit-only action permitted in KILL mode",
                reason_code="kill_exit_permitted",
            )
        return _build(
            Verdict.DENIED,
            f"jarvis_action=KILL -- {req.action.value} refused",
            reason_code="kill_blocks_all",
        )

    # 3. KILL_SWITCH_TRIP must always succeed.
    if req.action == ActionType.KILL_SWITCH_TRIP:
        return _build(
            Verdict.APPROVED,
            "kill-switch trip always allowed",
            reason_code="kill_trip_always",
        )

    # 4. STAND_ASIDE -- deny risk-adding; allow exit-only + modifications.
    if ctx.suggestion.action == ActionSuggestion.STAND_ASIDE:
        if req.action in _RISK_ADDING_ACTIONS:
            return _build(
                Verdict.DENIED,
                "jarvis_action=STAND_ASIDE -- no new risk "
                f"(binding: {ctx.stress_score.binding_constraint if ctx.stress_score else 'n/a'})",
                reason_code="stand_aside_blocks_risk",
            )
        # otherwise allow (modifications, exits)
        return _build(
            Verdict.APPROVED,
            "non-risk action permitted under STAND_ASIDE",
            reason_code="stand_aside_permits_nonrisk",
        )

    # 5. REDUCE -- approve risk-adding only CONDITIONAL with size cap.
    if ctx.suggestion.action == ActionSuggestion.REDUCE:
        if req.action in _RISK_ADDING_ACTIONS:
            cap = min(live_size, 0.50)
            return _build(
                Verdict.CONDITIONAL,
                f"REDUCE tier -- size capped at {cap:.0%}",
                reason_code="reduce_size_cap",
                conditions=[f"size_mult<={cap:.4f}", "no pyramiding"],
                size_cap_mult=cap,
            )
        return _build(
            Verdict.APPROVED,
            "non-risk action permitted under REDUCE",
            reason_code="reduce_permits_nonrisk",
        )

    # 6. REVIEW -- require acknowledgement in payload.
    if ctx.suggestion.action == ActionSuggestion.REVIEW:
        if req.action in _RISK_ADDING_ACTIONS:
            if not req.payload.get("review_acknowledged"):
                return _build(
                    Verdict.DEFERRED,
                    "REVIEW tier -- must set payload['review_acknowledged']=True first",
                    reason_code="review_ack_required",
                )
            cap = min(live_size, 0.75)
            return _build(
                Verdict.CONDITIONAL,
                f"REVIEW acknowledged -- size capped at {cap:.0%}",
                reason_code="review_acked_with_cap",
                conditions=[f"size_mult<={cap:.4f}", "3-trade probation"],
                size_cap_mult=cap,
            )
        return _build(
            Verdict.APPROVED,
            "non-risk action permitted under REVIEW",
            reason_code="review_permits_nonrisk",
        )

    # 7. Session gates (apply even when tier == TRADE).
    session = ctx.session_phase or SessionPhase.OVERNIGHT
    # All 24/7 crypto bots (L2 BTC hybrid, L3 multi-perp desk BTC/ETH/SOL,
    # L4 yield vault, legacy seed) plus the operator are whitelisted for
    # OVERNIGHT if they pass payload['overnight_explicit']=True. Everything
    # else -- notably the US-index futures bots -- must sit out.
    overnight_whitelist: frozenset[SubsystemId] = CRYPTO_24_7_BOTS | {SubsystemId.OPERATOR}
    if (
        session == SessionPhase.OVERNIGHT
        and req.action in _RISK_ADDING_ACTIONS
        and (req.subsystem not in overnight_whitelist or not req.payload.get("overnight_explicit"))
    ):
        return _build(
            Verdict.DENIED,
            "OVERNIGHT session refused for non-whitelisted subsystem (futures liquidity thin, wide spreads)",
            reason_code="overnight_refused",
        )
    if session == SessionPhase.CLOSE and req.action in {
        ActionType.ORDER_PLACE,
        ActionType.SIGNAL_EMIT,
    }:
        return _build(
            Verdict.DENIED,
            "CLOSE session (last 30min) -- no new entries",
            reason_code="close_no_new_entries",
        )

    # 8. TRADE tier -- APPROVED (with size cap from sizing_hint).
    return _build(
        Verdict.APPROVED,
        "all gates green; TRADE tier",
        reason_code="trade_ok",
        size_cap_mult=live_size,
    )


# ---------------------------------------------------------------------------
# JarvisAdmin -- the authority class
# ---------------------------------------------------------------------------


class JarvisAdmin:
    """Central authority. Every autonomous subsystem must call
    ``request_approval()`` before taking an action.

    Parameters
    ----------
    engine:
        A ``JarvisContextEngine`` that produces a fresh JarvisContext per
        request. If ``None``, caller must pass ``ctx`` to ``request_approval``.
    audit_path:
        Optional path to append-only JSONL file. Every (request, response)
        pair is appended. If ``None``, no file I/O.
    """

    def __init__(
        self,
        *,
        engine: JarvisContextEngine | None = None,
        audit_path: Path | None = None,
        policy_version: int = 0,
    ) -> None:
        """Construct a JarvisAdmin.

        ``policy_version`` (Lever 2, 2026-04-26): an integer label that's
        attached to every audit record this admin writes. Bump when JARVIS's
        decision logic changes (a kaizen-driven update gets promoted through
        the gate). Lets the replay engine compare v17-vs-v18 behavior over
        the same event stream.
        """
        self._engine = engine
        self._audit_path = audit_path
        self._policy_version = int(policy_version)
        if audit_path is not None:
            audit_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def policy_version(self) -> int:
        """Current JARVIS policy version. See __init__ docstring."""
        return self._policy_version

    def request_approval(
        self,
        req: ActionRequest,
        *,
        ctx: JarvisContext | None = None,
    ) -> ActionResponse:
        """Evaluate a request against the current Jarvis context.

        If ``ctx`` is provided, uses it directly (handy for deterministic
        tests). Otherwise ticks the engine to get a fresh context.

        Live-path policy selection (2026-04-27, pre-live plumbing):
          * Default: champion ``evaluate_request`` (v17 logic).
          * When feature flag ``V22_SAGE_MODULATION=true``, route through
            ``evaluate_v22`` so the multi-school sage confluence modulates
            the verdict on every order. v22 calls v17 internally and only
            modulates when ``req.payload['sage_bars']`` is present and
            sage conviction is high enough -- otherwise it returns the
            v17 verdict unchanged. So flipping the flag is safe even if
            not every bot is yet feeding sage_bars.
        """
        if ctx is None:
            if self._engine is None:
                raise RuntimeError(
                    "JarvisAdmin needs either an engine or an explicit ctx",
                )
            ctx = self._engine.tick()

        # Wave-6 pre-live plumbing: optionally route through v22 sage.
        # Lazy import + lazy flag check so we never pay the cost when
        # the flag is off.
        try:
            from eta_engine.brain.feature_flags import is_enabled as _ff_enabled
            sage_live = _ff_enabled("V22_SAGE_MODULATION")
        except Exception:  # noqa: BLE001 -- feature_flags import must never crash JARVIS
            sage_live = False

        if sage_live:
            try:
                from eta_engine.brain.jarvis_v3.policies.v22_sage_confluence import (
                    evaluate_v22,
                )
                resp = evaluate_v22(req, ctx)
            except Exception as exc:  # noqa: BLE001 -- fall back to champion on any failure
                logger.warning(
                    "v22_sage_confluence raised %s -- falling back to v17: %s",
                    type(exc).__name__,
                    exc,
                )
                resp = evaluate_request(req, ctx)
        else:
            resp = evaluate_request(req, ctx)

        self._audit(req, resp, ctx)
        return resp

    def _audit(
        self,
        req: ActionRequest,
        resp: ActionResponse,
        ctx: JarvisContext,
    ) -> None:
        if self._audit_path is None:
            return
        record = {
            "ts": resp.ts.isoformat(),
            "policy_version": self._policy_version,  # Lever 2 (2026-04-26)
            "request": req.model_dump(mode="json"),
            "response": resp.model_dump(mode="json"),
            "jarvis_action": ctx.suggestion.action.value,
            "stress_composite": (ctx.stress_score.composite if ctx.stress_score else None),
            "session_phase": (ctx.session_phase.value if ctx.session_phase else None),
            "explanation": ctx.explanation,
            "market_context": ctx.market_context,
            "market_context_summary": (
                ctx.market_context_summary or build_market_context_summary(ctx.model_dump(mode="json"))
            ),
            "market_context_summary_text": (
                ctx.market_context_summary_text
                or format_market_context_summary(
                    ctx.market_context_summary or build_market_context_summary(ctx.model_dump(mode="json"))
                )
            ),
        }
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def select_llm_tier(
        self,
        *,
        subsystem: SubsystemId,
        category: TaskCategory,
        rationale: str = "",
    ) -> ActionResponse:
        """Ergonomic wrapper for LLM-tier routing.

        Callers that just want "which model should I use for this task?"
        can skip building an ActionRequest by hand. LLM routing is
        stress-independent, so no ``JarvisContext`` is required -- this
        method works even when no engine is attached.

        The (request, response) pair is still appended to the audit log
        (if configured) so burn-rate dashboards can aggregate across
        subsystems.
        """
        req = ActionRequest(
            subsystem=subsystem,
            action=ActionType.LLM_INVOCATION,
            payload={"task_category": category.value},
            rationale=rationale,
        )
        resp = evaluate_llm_request(req)
        # Audit without requiring a live ctx -- synthesize a minimal one
        # only if we're actually writing to disk.
        if self._audit_path is not None:
            record = {
                "ts": resp.ts.isoformat(),
                "policy_version": self._policy_version,  # Lever 2 (2026-04-26)
                "request": req.model_dump(mode="json"),
                "response": resp.model_dump(mode="json"),
                "jarvis_action": "N/A (LLM routing)",
                "stress_composite": None,
                "session_phase": None,
                "explanation": resp.reason,
            }
            with self._audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        return resp

    def audit_tail(self, n: int = 20) -> list[dict[str, Any]]:
        """Read the last n audit records (for debugging / dashboards)."""
        if self._audit_path is None or not self._audit_path.exists():
            return []
        with self._audit_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        tail = lines[-n:] if n > 0 else lines
        return [json.loads(line) for line in tail if line.strip()]


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def make_action_request(
    *,
    subsystem: SubsystemId,
    action: ActionType,
    rationale: str = "",
    **payload: Any,  # noqa: ANN401 -- payload is deliberately untyped by design
) -> ActionRequest:
    """Short-form factory for subsystems that don't want pydantic ceremony."""
    return ActionRequest(
        subsystem=subsystem,
        action=action,
        rationale=rationale,
        payload=payload,
    )
