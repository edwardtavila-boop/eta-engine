"""APEX PREDATOR  //  bots.retrospective_adapter
======================================================
Adapter shims that translate the bots' native Signal/Fill/RegimeType
language into the retrospective system's StrategyId/RegimeLabel/
TradeOutcome language.

This isolation layer keeps the bot code free of strategy-taxonomy
concerns. The bot calls into a flat handful of pure-function helpers,
each of which can be swapped per-asset without rewriting the trading
loop.

Public surface:
    ActiveEntry                  -- per-symbol entry-tracking record
    DEFAULT_STRATEGY_FOR_BOT     -- symbol -> StrategyId mapping
    compute_risk_usd             -- equity * pct -> USD-at-risk
    default_strategy_for_symbol  -- DEFAULT_STRATEGY_FOR_BOT lookup
    is_entry_signal_type         -- True for LONG/SHORT, False for CLOSE/FLAT
    is_close_fill                -- fill that brings a position to zero
    map_regime                   -- RegimeType -> RegimeLabel
    build_trade_outcome          -- assembled TradeOutcome
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from apex_predator.bots.base_bot import SignalType
from apex_predator.strategies.adaptive_sizing import RegimeLabel
from apex_predator.strategies.models import StrategyId
from apex_predator.strategies.retrospective import TradeOutcome

if TYPE_CHECKING:
    from apex_predator.bots.base_bot import Fill, RegimeType


# ---------------------------------------------------------------------------
# Symbol -> default strategy mapping
# ---------------------------------------------------------------------------
DEFAULT_STRATEGY_FOR_BOT: dict[str, StrategyId] = {
    # Futures
    "MNQ":      StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
    "NQ":       StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
    # Crypto perps
    "ETH":      StrategyId.MTF_TREND_FOLLOWING,
    "ETHUSDT":  StrategyId.MTF_TREND_FOLLOWING,
    "ETH-PERP": StrategyId.MTF_TREND_FOLLOWING,
    "SOL":      StrategyId.MTF_TREND_FOLLOWING,
    "SOLUSDT":  StrategyId.MTF_TREND_FOLLOWING,
    "SOL-PERP": StrategyId.MTF_TREND_FOLLOWING,
    "XRP":      StrategyId.MTF_TREND_FOLLOWING,
    "XRPUSDT":  StrategyId.MTF_TREND_FOLLOWING,
    "XRP-PERP": StrategyId.MTF_TREND_FOLLOWING,
    # Crypto seed grid
    "BTC":      StrategyId.REGIME_ADAPTIVE_ALLOCATION,
    "BTCUSDT":  StrategyId.REGIME_ADAPTIVE_ALLOCATION,
}

_FALLBACK_STRATEGY: StrategyId = StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT


@dataclass(frozen=True)
class ActiveEntry:
    """Per-symbol entry-tracking record for retrospective bookkeeping."""
    symbol: str
    risk_usd: float
    strategy: StrategyId
    regime: RegimeLabel
    opened_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))


def compute_risk_usd(*, equity: float, risk_per_trade_pct: float) -> float:
    """USD-at-risk for one trade given the bot's equity + risk budget."""
    if equity <= 0.0 or risk_per_trade_pct <= 0.0:
        return 0.0
    return equity * (risk_per_trade_pct / 100.0)


def default_strategy_for_symbol(symbol: str) -> StrategyId:
    """Look up the bot family's default strategy. Falls back to sweep."""
    if not symbol:
        return _FALLBACK_STRATEGY
    return DEFAULT_STRATEGY_FOR_BOT.get(symbol.upper(), _FALLBACK_STRATEGY)


def is_entry_signal_type(signal_type: SignalType) -> bool:
    """LONG/SHORT are entries; CLOSE/FLAT are not."""
    return signal_type in (SignalType.LONG, SignalType.SHORT)


def is_close_fill(fill: Fill) -> bool:
    """A fill that closes a position.

    Conservatively detects close fills via:
      * non-zero realized_pnl (a position-flat fill always realizes pnl)
      * side == "CLOSE" (operator-provided explicit hint)
    """
    side = (fill.side or "").upper()
    if side == "CLOSE":
        return True
    return fill.realized_pnl != 0.0


_REGIME_MAP: dict[str, RegimeLabel] = {
    "TRENDING":   RegimeLabel.TRENDING,
    "RANGING":    RegimeLabel.RANGING,
    "TRANSITION": RegimeLabel.TRANSITION,
    "HIGH_VOL":   RegimeLabel.HIGH_VOL,
}


def map_regime(regime: RegimeType) -> RegimeLabel:
    """Project the bot-side ``RegimeType`` enum onto the retrospective
    ``RegimeLabel``. Unknown labels degrade to ``TRANSITION``.
    """
    name = getattr(regime, "name", None) or getattr(regime, "value", None) or str(regime)
    return _REGIME_MAP.get(str(name).upper(), RegimeLabel.TRANSITION)


def build_trade_outcome(
    *,
    strategy: StrategyId,
    regime: RegimeLabel,
    pnl_r: float,
    equity_after: float,
) -> TradeOutcome:
    """Assemble a :class:`TradeOutcome` for the manager's ``record_trade``."""
    return TradeOutcome(
        strategy=strategy,
        regime=regime,
        pnl_r=pnl_r,
        equity_after=equity_after,
    )


__all__ = [
    "DEFAULT_STRATEGY_FOR_BOT",
    "ActiveEntry",
    "build_trade_outcome",
    "compute_risk_usd",
    "default_strategy_for_symbol",
    "is_close_fill",
    "is_entry_signal_type",
    "map_regime",
]
