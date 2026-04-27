"""ETA Engine // brain.jarvis_pre_flight
========================================
Combined bot pre-flight helper (2026-04-27).

Bots already have ``self._ask_jarvis(...)`` to gate every order through
JARVIS. This module bolts two more checks on top so each bot opts into
them with a one-line call instead of duplicating the wiring 7 times:

  1. Cross-bot correlation throttle
     (``jarvis_correlation.should_throttle_for_correlation``)
  2. Standard ``ActionType.ORDER_PLACE`` request through JARVIS
  3. ``realized_r`` plumbing for the kaizen P&L feedback (bots call
     ``record_fill_with_realized_r`` when a trade closes -- the journal
     event then carries the metadata the kaizen synthesizer reads)

Drop-in usage from a bot::

    from eta_engine.brain.jarvis_pre_flight import bot_pre_flight, record_fill_with_realized_r

    decision = bot_pre_flight(
        bot=self,
        symbol=signal.symbol,
        side=signal.side,
        confluence=signal.confidence,
        fleet_positions=fleet.positions_by_symbol(),
    )
    if not decision.allowed:
        return None  # blocked or deferred
    qty *= decision.size_cap_mult
    ...
    fill = await self.router.place_with_failover(...)
    ...
    # When the trade closes:
    record_fill_with_realized_r(self._journal, intent, r_multiple=R)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightDecision:
    """Aggregated verdict of correlation throttle + JARVIS gate."""

    allowed: bool
    size_cap_mult: float
    reason: str
    reason_code: str
    binding: str  # "correlation" | "jarvis" | "approved"


def bot_pre_flight(
    *,
    bot: Any,  # noqa: ANN401 -- duck-typed bot (BaseBot subclass or stub)
    symbol: str,
    side: str,
    confluence: float,
    fleet_positions: Mapping[str, float],
    rationale: str = "",
    extra_payload: dict[str, Any] | None = None,
) -> PreflightDecision:
    """Run the combined pre-flight: correlation -> JARVIS.

    Bot must have ``self._ask_jarvis(action, **payload) -> (allowed, cap, code)``
    available (every base bot already does -- see ``bots/mnq/bot.py``).
    """
    from eta_engine.brain.jarvis_admin import ActionType
    from eta_engine.brain.jarvis_correlation import should_throttle_for_correlation

    # 1. Correlation throttle (CHEAP -- just a dict lookup; no LLM)
    corr = should_throttle_for_correlation(symbol, fleet_positions)
    if corr.cap_mult <= 0.0:
        return PreflightDecision(
            allowed=False,
            size_cap_mult=0.0,
            reason=corr.detail,
            reason_code=corr.reason_code,
            binding="correlation",
        )

    # 2. JARVIS gate (ORDER_PLACE)
    if not hasattr(bot, "_ask_jarvis"):
        # Bot didn't opt in -- pass through with correlation cap applied
        return PreflightDecision(
            allowed=True,
            size_cap_mult=corr.cap_mult,
            reason=f"correlation_only ({corr.reason_code})",
            reason_code="no_jarvis_attached",
            binding="correlation" if corr.cap_mult < 1.0 else "approved",
        )

    payload = dict(extra_payload or {})
    payload.update({
        "side": side,
        "symbol": symbol,
        "confidence": confluence,
    })
    if rationale:
        payload["rationale"] = rationale
    # Wave-6 (2026-04-27): auto-attach sage_bars when the bot maintains
    # a sage-bar history. v22_sage_confluence picks them up from
    # payload['sage_bars'] when V22_SAGE_MODULATION=true. Bots that
    # don't opt in (no recent_sage_bars method or empty buffer) skip
    # this step -- sage falls back to v17 silently.
    if "sage_bars" not in payload and hasattr(bot, "recent_sage_bars"):
        try:
            sage_bars = bot.recent_sage_bars()
            if sage_bars:
                payload["sage_bars"] = sage_bars
        except Exception:  # noqa: BLE001 -- never break the trading loop
            pass
    allowed, jarvis_cap, code = bot._ask_jarvis(ActionType.ORDER_PLACE, **payload)
    if not allowed:
        return PreflightDecision(
            allowed=False,
            size_cap_mult=0.0,
            reason=f"jarvis blocked: {code}",
            reason_code=code or "jarvis_denied",
            binding="jarvis",
        )

    # Compose caps: correlation × JARVIS (both pessimistic)
    final_cap = corr.cap_mult
    if jarvis_cap is not None and jarvis_cap < final_cap:
        final_cap = jarvis_cap
    return PreflightDecision(
        allowed=True,
        size_cap_mult=final_cap,
        reason=f"approved (corr={corr.cap_mult:.2f}, jarvis_cap={jarvis_cap})",
        reason_code="approved",
        binding="approved" if final_cap >= 1.0 else (
            "correlation" if final_cap == corr.cap_mult else "jarvis"
        ),
    )


def record_fill_with_realized_r(
    journal: Any,  # noqa: ANN401 -- duck-typed DecisionJournal
    intent: str,
    *,
    r_multiple: float,
    bot_name: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a journal event with ``metadata['realized_r']`` populated.

    The kaizen synthesizer (run_kaizen_close_cycle.synthesize_inputs)
    looks for this field to ground went_well/went_poorly in money
    outcomes rather than just gate firings (Tier-2 #7, 2026-04-27).

    ``journal`` must be a ``DecisionJournal`` (or a compatible duck:
    needs a ``record(*, actor, intent, outcome, metadata)`` method).
    """
    from eta_engine.obs.decision_journal import Actor, Outcome

    metadata = {"realized_r": float(r_multiple)}
    if bot_name:
        metadata["bot_name"] = bot_name
    if extra:
        metadata.update(extra)
    try:
        journal.record(
            actor=Actor.TRADE_ENGINE,
            intent=intent,
            outcome=Outcome.NOTED,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("realized_r journal append failed (non-fatal): %s", exc)
