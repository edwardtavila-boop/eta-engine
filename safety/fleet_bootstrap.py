"""EVOLUTIONARY TRADING ALGO // safety.fleet_bootstrap.

One-call wiring layer for the fleet-risk primitives. At
orchestrator startup, after constructing the bot fleet::

    from eta_engine.safety.fleet_bootstrap import bootstrap_fleet_risk

    bots = [mnq_bot, btc_bot, eth_bot, sol_bot, seed_bot]
    gate = bootstrap_fleet_risk(bots)

Steps performed:
  1. Sums ``starting_capital_usd`` across the bots passed in
     (or the registry default if none) and constructs a single
     :class:`FleetRiskGate` sized for the fleet.
  2. Registers it as the process-wide singleton so every venue
     client's ``assert_fleet_within_budget()`` call sees it.
  3. Attaches the same gate to every bot via
     :meth:`BaseBot.attach_fleet_risk_gate` so each closing fill
     feeds the running aggregate.

Without this helper the operator has to do all three steps in
the right order at startup; forgetting any one leaves the gate
disarmed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eta_engine.safety.fleet_risk_gate import (
    FleetRiskGate,
    register_fleet_risk_gate,
)
from eta_engine.strategies.per_bot_registry import (
    all_assignments,
    get_for_bot,
    is_active,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eta_engine.bots.base_bot import BaseBot


def _sum_active_starting_equity() -> float:
    """Default fleet equity when no bot list passed.

    The per-bot registry doesn't carry starting equity (that lives on
    each ``BotConfig``), so this fallback assumes 10_000 USD per
    active bot. Operators relying on this fallback should override
    via ``APEX_FLEET_DAILY_LOSS_LIMIT_USD`` or pass an explicit
    ``equity_override``.
    """
    active = [a for a in all_assignments() if is_active(a)]
    return float(len(active)) * 10_000.0


def bootstrap_fleet_risk(
    bots: Iterable[BaseBot] | None = None,
    *,
    equity_override: float | None = None,
    register_singleton: bool = True,
    attach_to_bots: bool = True,
) -> FleetRiskGate:
    """Construct + wire a fleet-wide :class:`FleetRiskGate`.

    Steps performed in order:

      1. Compute fleet starting equity.
         * If ``equity_override`` is set, use it verbatim.
         * Else if ``bots`` is passed, sum each bot's
           ``config.starting_capital_usd``.
         * Else fall back to a 10_000-USD-per-active-bot default.
      2. Construct a :class:`FleetRiskGate`.
      3. Register as process singleton (skip with ``register_singleton=False``).
      4. Attach to each bot in ``bots`` (skip with ``attach_to_bots=False``).
    """
    if equity_override is not None and equity_override > 0:
        equity = float(equity_override)
    elif bots is not None:
        bot_list = list(bots)
        if not bot_list:
            equity = _sum_active_starting_equity()
        else:
            equity = sum(
                float(getattr(b.config, "starting_capital_usd", 0.0))
                for b in bot_list
            )
    else:
        equity = _sum_active_starting_equity()

    if equity <= 0:
        raise ValueError(
            f"computed fleet equity is non-positive ({equity}); pass "
            "equity_override or a bots list with starting_capital_usd "
            "set on each config."
        )

    gate = FleetRiskGate(fleet_starting_equity_usd=equity)

    if register_singleton:
        register_fleet_risk_gate(gate)

    if attach_to_bots and bots is not None:
        for bot in bots:
            bot_id = getattr(bot.config, "name", None)
            if bot_id is not None and get_for_bot(bot_id) is None:
                import logging
                logging.getLogger(__name__).warning(
                    "bootstrap_fleet_risk: bot %r has no registry "
                    "entry; PnL will accumulate under that name "
                    "anyway, but it won't be visible to the "
                    "drift_monitor / correlation_watchdog.",
                    bot_id,
                )
            bot.attach_fleet_risk_gate(gate, bot_id=bot_id)

    return gate
