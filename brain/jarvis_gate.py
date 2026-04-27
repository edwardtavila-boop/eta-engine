"""
EVOLUTIONARY TRADING ALGO // brain.jarvis_gate
==================================
Shared JARVIS gating helpers for the bot fleet.

Every bot that wants JARVIS to gate its risk-adding actions goes through
this module. It provides three things:

* :func:`ask_jarvis` -- one-call gate around
  :meth:`JarvisAdmin.request_approval` that unpacks the verdict into
  ``(allowed, size_cap_mult, reason_code)``.
* :func:`record_gate_event` -- one-call :class:`DecisionJournal` writer
  that never crashes the caller on journal I/O errors.
* :func:`pick_llm_tier` -- one-call wrapper around
  :meth:`JarvisAdmin.select_llm_tier` for bots that need model-tier
  routing (e.g. "Which tier should I use for this next refactor?").

The BTC hybrid bot (``bots/btc_hybrid/bot.py``) carries its own inline
copy of this logic for legacy reasons; new bots should prefer this
module. The two implementations are intentionally kept in sync via
the test suite.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from eta_engine.brain.jarvis_admin import (
    ActionType,
    JarvisAdmin,
    SubsystemId,
    Verdict,
    make_action_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.brain.jarvis_context import JarvisContext
    from eta_engine.brain.model_policy import ModelTier, TaskCategory
    from eta_engine.obs.decision_journal import Actor, DecisionJournal, Outcome


logger = logging.getLogger(__name__)


# The two verdicts under which a bot is allowed to proceed. CONDITIONAL
# additionally carries a ``size_cap_mult`` that the caller must respect.
_ALLOWED_VERDICTS = frozenset({Verdict.APPROVED, Verdict.CONDITIONAL})


def ask_jarvis(
    jarvis: JarvisAdmin,
    *,
    subsystem: SubsystemId,
    action: ActionType,
    rationale: str = "",
    provide_ctx: Callable[[], JarvisContext] | None = None,
    log_name: str = "",
    **payload: Any,  # noqa: ANN401 -- payload is deliberately free-form
) -> tuple[bool, float | None, str]:
    """Gate a risk-adding action through JARVIS.

    Parameters
    ----------
    jarvis:
        The :class:`JarvisAdmin` instance that will evaluate the request.
    subsystem:
        Which subsystem is asking (BOT_MNQ, BOT_BTC_HYBRID, ...).
    action:
        The :class:`ActionType` being requested (ORDER_PLACE,
        STRATEGY_DEPLOY, ...).
    rationale:
        One-sentence explanation logged to the audit trail.
    provide_ctx:
        Optional zero-arg callable returning a fresh
        :class:`JarvisContext`. When omitted, JARVIS ticks its engine
        (which requires an engine to have been attached at construction).
    log_name:
        Short human-readable identifier for log prefixes
        (e.g. ``"MNQ-Engine"``). Optional -- purely cosmetic.
    **payload:
        Action-specific payload forwarded verbatim into the request
        (``side``, ``qty``, ``symbol``, ``confidence``, ...).

    Returns
    -------
    tuple[bool, float | None, str]
        ``(allowed, size_cap_mult, reason_code)``.

        * ``allowed`` -- True on APPROVED / CONDITIONAL, False on
          DENIED / DEFERRED.
        * ``size_cap_mult`` -- Optional size multiplier from
          CONDITIONAL; ``None`` for APPROVED or non-allowed verdicts.
        * ``reason_code`` -- Stable machine-readable reason code from
          JARVIS. Always set, whether allowed or not.
    """
    req = make_action_request(
        subsystem=subsystem,
        action=action,
        rationale=rationale,
        **payload,
    )
    ctx = provide_ctx() if provide_ctx is not None else None
    try:
        resp = jarvis.request_approval(req, ctx=ctx)
    except Exception as exc:  # noqa: BLE001 - fail-closed on unexpected errors
        logger.error(
            "%s jarvis.request_approval raised %s: %s -- failing closed",
            log_name or subsystem.value,
            type(exc).__name__,
            exc,
        )
        return False, None, "jarvis_error"
    allowed = resp.verdict in _ALLOWED_VERDICTS
    prefix = log_name or subsystem.value
    if not allowed:
        logger.info(
            "%s jarvis refused %s: %s (%s)",
            prefix,
            action.value,
            resp.reason,
            resp.reason_code,
        )
    elif resp.verdict is Verdict.CONDITIONAL:
        logger.info(
            "%s jarvis conditional %s: size_cap=%.3f (%s)",
            prefix,
            action.value,
            resp.size_cap_mult if resp.size_cap_mult is not None else 1.0,
            resp.reason_code,
        )
    return allowed, resp.size_cap_mult, resp.reason_code


def record_gate_event(
    journal: DecisionJournal | None,
    *,
    actor: Actor,
    intent: str,
    rationale: str = "",
    outcome: Outcome | None = None,
    log_name: str = "",
    **metadata: Any,  # noqa: ANN401 -- journal payloads are deliberately flexible
) -> None:
    """Append a journal event, silently swallowing I/O failures.

    ``None`` journal is a valid no-op. Exceptions from the writer
    never propagate to the caller -- a dead disk must not crash the
    trading loop.
    """
    if journal is None:
        return
    # Local imports so this module stays importable in environments
    # that haven't wired the obs subpackage yet.
    from eta_engine.obs.decision_journal import Outcome as _Outcome

    final_outcome = outcome if outcome is not None else _Outcome.NOTED
    try:
        journal.record(
            actor=actor,
            intent=intent,
            rationale=rationale,
            outcome=final_outcome,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001 - journal errors never kill trading
        logger.warning(
            "%s journal write failed: %s",
            log_name or actor.value,
            exc,
        )


def pick_llm_tier(
    jarvis: JarvisAdmin,
    *,
    subsystem: SubsystemId,
    category: TaskCategory,
    rationale: str = "",
) -> ModelTier:
    """Ask JARVIS which model tier to use for a given task category.

    Thin wrapper around :meth:`JarvisAdmin.select_llm_tier`. Returns
    only the model tier -- callers that need the full
    :class:`ActionResponse` should call the admin method directly.

    LLM routing is stress-independent so this works even when JARVIS
    has no engine attached.
    """
    # Local import to avoid pulling model_policy at module load time.
    from eta_engine.brain.model_policy import ModelTier as _ModelTier

    try:
        resp = jarvis.select_llm_tier(
            subsystem=subsystem,
            category=category,
            rationale=rationale,
        )
    except Exception as exc:  # noqa: BLE001 - default to SONNET on any error
        logger.warning(
            "%s select_llm_tier raised %s: %s -- defaulting to SONNET",
            subsystem.value,
            type(exc).__name__,
            exc,
        )
        return _ModelTier.SONNET
    # Prefer the typed selected_model field; fall back to SONNET on
    # DEFERRED (missing payload) or if JARVIS returned nothing parseable.
    return resp.selected_model or _ModelTier.SONNET


__all__ = [
    "ask_jarvis",
    "pick_llm_tier",
    "record_gate_event",
]
