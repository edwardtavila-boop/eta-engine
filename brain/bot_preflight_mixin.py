"""BotPreFlightMixin -- one-line per-bot integration (wave-6, 2026-04-27).

Each production bot already calls ``self._ask_jarvis(...)`` before
order placement. To wire bot_pre_flight() (which adds correlation
throttle on top of JARVIS), the bot would need its on_signal flow
edited. That's risky to batch.

This mixin is the SAFE alternative: each bot just adds it to its
class hierarchy + reads the new ``self.gate_or_block(...)`` helper in
its on_signal flow. One line per bot::

    class MnqBot(BotPreFlightMixin, BaseBot):
        async def on_signal(self, signal: Signal) -> ...:
            decision = self.gate_or_block(
                symbol=signal.symbol,
                side=signal.type.value,
                confluence=signal.confidence,
                fleet_positions=self._fleet_positions_callback(),
            )
            if not decision.allowed:
                return None
            qty = base_qty * decision.size_cap_mult
            ...

Doesn't replace ``_ask_jarvis`` -- the JARVIS gate inside
``bot_pre_flight`` IS ``_ask_jarvis``. The mixin just adds:
  1. correlation throttle (cheap dict lookup, no LLM)
  2. feature-flag check (PER_BOT_PRE_FLIGHT must be on; otherwise
     mixin behaves exactly like _ask_jarvis -- backward-compatible)
  3. consistent decision return shape across all bots
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from eta_engine.brain.jarvis_pre_flight import PreflightDecision

logger = logging.getLogger(__name__)


class BotPreFlightMixin:
    """Mixin that gives a bot a one-call pre-flight gate.

    Requires the bot to also inherit from ``BaseBot`` so ``run_pre_flight``
    is available. Idempotent: bots that already added the mixin and
    re-add it suffer no ill effect.
    """

    def gate_or_block(
        self,
        *,
        symbol: str,
        side: str,
        confluence: float,
        fleet_positions: Mapping[str, float] | None = None,
        rationale: str = "",
        extra_payload: dict[str, object] | None = None,
    ) -> PreflightDecision:
        """Check pre-flight; if PER_BOT_PRE_FLIGHT flag is OFF, fall back
        to the bot's existing ``_ask_jarvis`` flow.

        Returns the same ``PreflightDecision`` shape regardless of which
        path is taken so on_signal code can stay uniform.
        """
        from eta_engine.brain.feature_flags import is_enabled
        from eta_engine.brain.jarvis_pre_flight import PreflightDecision

        # Default-OFF: legacy behavior preserved
        if not is_enabled("PER_BOT_PRE_FLIGHT"):
            # Mimic the PreflightDecision shape using the bot's existing
            # _ask_jarvis flow so the consumer code stays uniform.
            from eta_engine.brain.jarvis_admin import ActionType

            payload = dict(extra_payload or {})
            payload.update({"side": side, "symbol": symbol, "confidence": confluence})
            if rationale:
                payload["rationale"] = rationale
            if not hasattr(self, "_ask_jarvis"):
                # Bot has neither _ask_jarvis nor opted into pre-flight.
                # Default: allow with full size.
                return PreflightDecision(
                    allowed=True,
                    size_cap_mult=1.0,
                    reason="no jarvis attached",
                    reason_code="no_jarvis",
                    binding="approved",
                )
            allowed, cap, code = self._ask_jarvis(ActionType.ORDER_PLACE, **payload)
            return PreflightDecision(
                allowed=bool(allowed),
                size_cap_mult=float(cap) if cap is not None else 1.0,
                reason="legacy _ask_jarvis path",
                reason_code=code or "ok",
                binding="jarvis" if not allowed else "approved",
            )

        # Flag ON: full pre-flight (correlation + JARVIS)
        return self.run_pre_flight(  # type: ignore[attr-defined]
            symbol=symbol,
            side=side,
            confluence=confluence,
            fleet_positions=dict(fleet_positions or {}),
            rationale=rationale,
            extra_payload=extra_payload,
        )
