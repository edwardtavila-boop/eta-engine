"""EVOLUTIONARY TRADING ALGO // bots.retrospective_adapter.

Adapter helpers shared by every bot that participates in the v0.1.50
retrospective pipeline. The bot stashes an :class:`ActiveEntry` when
an entry signal fires; on the matching close fill the bot pops the
entry, computes ``pnl_r = realized_pnl / risk_usd``, and feeds the
retrospective manager.

This module is the small, pure layer that lets MNQ + NQ + ETH + SOL +
XRP share the same risk / strategy / regime mapping logic without
inheriting it through the perp class hierarchy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from eta_engine.bots.base_bot import RegimeType, SignalType
from eta_engine.strategies.adaptive_sizing import RegimeLabel
from eta_engine.strategies.models import StrategyId

if TYPE_CHECKING:
    from datetime import datetime

    from eta_engine.bots.base_bot import Fill


_ENTRY_SIGNAL_TYPES: frozenset[SignalType] = frozenset(
    {SignalType.LONG, SignalType.SHORT}
)


_REGIME_MAP: dict[RegimeType, RegimeLabel] = {
    RegimeType.TRENDING: RegimeLabel.TRENDING,
    RegimeType.RANGING: RegimeLabel.RANGING,
    RegimeType.TRANSITION: RegimeLabel.TRANSITION,
}


_DEFAULT_STRATEGY_BY_PREFIX: dict[str, StrategyId] = {
    # Index futures default to liquidity-sweep displacement (the
    # highest-edge MNQ strategy from the founder brief).
    "MNQ": StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
    "NQ": StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
    # Crypto perps default to OB breaker / retest (the second-ranked
    # strategy, which is the workhorse for trending crypto tape).
    "BTC": StrategyId.OB_BREAKER_RETEST,
    "ETH": StrategyId.OB_BREAKER_RETEST,
    "SOL": StrategyId.OB_BREAKER_RETEST,
    "XRP": StrategyId.OB_BREAKER_RETEST,
}


@dataclass(frozen=True, slots=True)
class ActiveEntry:
    """Snapshot of an open entry, stored until the close fill arrives.

    The bot stamps one of these on every entry signal and pops it on
    the matching close fill. Fields:

    * ``risk_usd`` -- dollars at risk on this entry (used to
      compute ``pnl_r``).
    * ``strategy`` -- ``StrategyId`` the trade should be attributed
      to in the retrospective.
    * ``regime`` -- ``RegimeLabel`` in force at the moment of entry.
    * ``opened_at_utc`` -- entry timestamp (UTC) for latency / hold-
      time stratification.
    """

    symbol: str
    risk_usd: float
    strategy: StrategyId
    regime: RegimeLabel
    opened_at_utc: datetime


def compute_risk_usd(*, equity: float, risk_per_trade_pct: float) -> float:
    """Return the USD risk for a fresh entry.

    Returns ``0.0`` (a sentinel meaning "no retrospective for this
    trade") whenever equity or risk_per_trade_pct are non-positive.
    The bot treats a zero result as "skip retrospective".
    """
    if equity <= 0.0 or risk_per_trade_pct <= 0.0:
        return 0.0
    # risk_per_trade_pct is a percent (e.g. 1.5 means 1.5%), not a fraction.
    return equity * (risk_per_trade_pct / 100.0)


def is_entry_signal_type(signal_type: SignalType) -> bool:
    """True for LONG / SHORT entry signals; False for closes / grid ops."""
    return signal_type in _ENTRY_SIGNAL_TYPES


def map_regime(regime: RegimeType) -> RegimeLabel:
    """Translate the bot-level :class:`RegimeType` to the strategy
    layer's :class:`RegimeLabel`. Unknown values fall back to
    ``RegimeLabel.TRANSITION`` so the retrospective always has a
    label to bucket against.
    """
    return _REGIME_MAP.get(regime, RegimeLabel.TRANSITION)


def default_strategy_for_symbol(symbol: str) -> StrategyId:
    """Return the canonical strategy attribution for ``symbol``.

    Looks up the longest matching prefix in
    :data:`_DEFAULT_STRATEGY_BY_PREFIX`; falls back to
    :class:`StrategyId.MTF_TREND_FOLLOWING` (a regime-agnostic
    strategy) for unknown symbols.
    """
    sym_upper = symbol.upper()
    for prefix in sorted(_DEFAULT_STRATEGY_BY_PREFIX, key=len, reverse=True):
        if sym_upper.startswith(prefix):
            return _DEFAULT_STRATEGY_BY_PREFIX[prefix]
    return StrategyId.MTF_TREND_FOLLOWING


def is_close_fill(fill: Fill) -> bool:
    """True when the fill represents a position close.

    The contract: a fill carries non-zero ``realized_pnl`` exactly
    when it closes (or partially closes) a position. Opening fills
    have ``realized_pnl == 0``.
    """
    return abs(float(fill.realized_pnl)) > 1e-12


__all__ = [
    "ActiveEntry",
    "compute_risk_usd",
    "default_strategy_for_symbol",
    "is_close_fill",
    "is_entry_signal_type",
    "map_regime",
]
