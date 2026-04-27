"""EVOLUTIONARY TRADING ALGO  //  strategies package.

AI-optimized SMC/ICT playbook distilled from the founder-brief
"Recommended Strategies (AI-Optimized Evolutionary Trading Algo List)".

Public surface:
  * ``models``            -- :class:`Bar`, :class:`StrategySignal`,
                            :class:`StrategyId`, :class:`Side`.
  * ``smc_primitives``    -- pure bar-level detectors
                            (FVG, OB, BOS, displacement, liquidity sweep).
  * ``eta_policy``       -- 6 named strategies composed from primitives.
  * ``policy_router``     -- per-asset dispatch, picks the best signal.
  * ``regime_allocator``  -- regime-adaptive top-level layer weights.
  * ``rl_policy``         -- thin wrapper around :mod:`brain.rl_agent`.

Design intent: every detector is a pure function of a list of bars.
No I/O. No hidden state. That keeps them composable with the existing
``funnel.waterfall``, ``brain.jarvis_admin``, and the bot fleet.
"""

from __future__ import annotations

from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)

__all__ = [
    "Bar",
    "Side",
    "StrategyId",
    "StrategySignal",
]
