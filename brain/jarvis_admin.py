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

# ---------------------------------------------------------------------------
# Subsystem registry -- every autonomous actor in the fleet
# ---------------------------------------------------------------------------


class SubsystemId(StrEnum):
    """Every autonomous action-taking component that must report to Jarvis.

    Keep these stable -- they end up as labels in the audit log.
    """
    # eta_engine bot fleet (portfolio)
    BOT_CRYPTO_SEED     = "bot.crypto_seed"
    BOT_ETH_PERP        = "bot.eth_perp"
    BOT_MNQ             = "bot.mnq"
    BOT_NQ              = "bot.nq"

    # mnq_bot v3 framework
    FRAMEWORK_AUTOPILOT        = "framework.autopilot"
    FRAMEWORK_FIRM_ENGINE      = "framework.firm_engine"
    FRAMEWORK_COURT_OF_APPEALS = "framework.court_of_appeals"
    FRAMEWORK_CONFLUENCE       = "framework.confluence_scorer"
    FRAMEWORK_WEBHOOK          = "framework.webhook"
    FRAMEWORK_META_ORCH        = "framework.meta_orchestrator"

    # the_firm 6-agent adversarial system
    AGENT_QUANT    = "firm.quant"
    AGENT_RED_TEAM = "firm.red_team"
    AGENT_RISK     = "firm.risk"
    AGENT_MACRO    = "firm.macro"
    AGENT_MICRO    = "firm.micro"
    AGENT_PM       = "firm.pm"

    # gate + watchdog + telemetry
    GATE_CHAIN        = "gates.chain"
    AUTOPILOT_WATCHDOG = "watchdog.autopilot"

    # operator (still must report when exercising override authority)
    OPERATOR = "operator.edward"


# ---------------------------------------------------------------------------
# Action taxonomy
# ---------------------------------------------------------------------------


class ActionType(StrEnum):
    """Kind of autonomous action a subsystem is requesting approval for."""
    # signal / decision lifecycle
    SIGNAL_EMIT       = "SIGNAL_EMIT"       # strategy produces a signal
    # order lifecycle
    ORDER_PLACE       = "ORDER_PLACE"
    ORDER_MODIFY      = "ORDER_MODIFY"      # move stop, change size, etc.
    ORDER_CANCEL      = "ORDER_CANCEL"
    POSITION_FLATTEN  = "POSITION_FLATTEN"  # emergency exit one position
    # system-level
    KILL_SWITCH_TRIP  = "KILL_SWITCH_TRIP"
    KILL_SWITCH_RESET = "KILL_SWITCH_RESET" # operator only
    AUTOPILOT_RESUME  = "AUTOPILOT_RESUME"
    GATE_OVERRIDE     = "GATE_OVERRIDE"     # operator overriding a blocking gate
    # strategy / portfolio lifecycle
    STRATEGY_DEPLOY   = "STRATEGY_DEPLOY"   # promote paper -> live
    STRATEGY_RETIRE   = "STRATEGY_RETIRE"
    PARAMETER_CHANGE  = "PARAMETER_CHANGE"  # tune size/stop/target
    CAPITAL_ALLOCATE  = "CAPITAL_ALLOCATE"  # move capital between bots


class Verdict(StrEnum):
    APPROVED    = "APPROVED"
    CONDITIONAL = "CONDITIONAL"   # approved WITH conditions (size cap, etc.)
    DENIED      = "DENIED"
    DEFERRED    = "DEFERRED"      # try again later (e.g. wait for macro event)


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
        description="One-sentence explanation of why the subsystem wants "
                    "this action. Logged for post-hoc review.",
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
        default=None, ge=0.0, le=1.0,
        description="Max size multiplier approved (None = no explicit cap).",
    )
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


# Actions that are ALWAYS permitted (protective / exit-only).
_EXIT_ONLY_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.ORDER_CANCEL,
    ActionType.POSITION_FLATTEN,
    ActionType.KILL_SWITCH_TRIP,
})

# Actions that grow risk (net-new exposure).
_RISK_ADDING_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.SIGNAL_EMIT,
    ActionType.ORDER_PLACE,
    ActionType.STRATEGY_DEPLOY,
    ActionType.CAPITAL_ALLOCATE,
})

# Operator-only -- no bot may trigger these.
_OPERATOR_ONLY: frozenset[ActionType] = frozenset({
    ActionType.KILL_SWITCH_RESET,
    ActionType.GATE_OVERRIDE,
    ActionType.AUTOPILOT_RESUME,
})


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
            stress_composite=(
                ctx.stress_score.composite if ctx.stress_score else 0.0
            ),
            session_phase=(ctx.session_phase or SessionPhase.OVERNIGHT),
            binding_constraint=(
                ctx.stress_score.binding_constraint
                if ctx.stress_score else ""
            ),
            size_cap_mult=size_cap_mult,
        )

    live_size = (
        ctx.sizing_hint.size_mult if ctx.sizing_hint is not None else 1.0
    )

    # 1. Operator-only actions -- non-operators are refused.
    if (
        req.action in _OPERATOR_ONLY
        and req.subsystem != SubsystemId.OPERATOR
    ):
        return _build(
            Verdict.DENIED,
            f"{req.action.value} is operator-only; "
            f"{req.subsystem.value} not authorized",
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
                    "REVIEW tier -- must set "
                    "payload['review_acknowledged']=True first",
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
    overnight_whitelist: set[SubsystemId] = {
        SubsystemId.BOT_CRYPTO_SEED,
        SubsystemId.BOT_ETH_PERP,
        SubsystemId.OPERATOR,
    }
    if (
        session == SessionPhase.OVERNIGHT
        and req.action in _RISK_ADDING_ACTIONS
        and (
            req.subsystem not in overnight_whitelist
            or not req.payload.get("overnight_explicit")
        )
    ):
        return _build(
            Verdict.DENIED,
            "OVERNIGHT session refused for non-whitelisted subsystem "
            "(futures liquidity thin, wide spreads)",
            reason_code="overnight_refused",
        )
    if session == SessionPhase.CLOSE and req.action in {
        ActionType.ORDER_PLACE, ActionType.SIGNAL_EMIT,
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
    ) -> None:
        self._engine = engine
        self._audit_path = audit_path
        if audit_path is not None:
            audit_path.parent.mkdir(parents=True, exist_ok=True)

    def request_approval(
        self,
        req: ActionRequest,
        *,
        ctx: JarvisContext | None = None,
    ) -> ActionResponse:
        """Evaluate a request against the current Jarvis context.

        If ``ctx`` is provided, uses it directly (handy for deterministic
        tests). Otherwise ticks the engine to get a fresh context.
        """
        if ctx is None:
            if self._engine is None:
                raise RuntimeError(
                    "JarvisAdmin needs either an engine or an explicit ctx",
                )
            ctx = self._engine.tick()
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
            "request": req.model_dump(mode="json"),
            "response": resp.model_dump(mode="json"),
            "jarvis_action": ctx.suggestion.action.value,
            "stress_composite": (
                ctx.stress_score.composite if ctx.stress_score else None
            ),
            "session_phase": (
                ctx.session_phase.value if ctx.session_phase else None
            ),
            "explanation": ctx.explanation,
        }
        with self._audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

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
