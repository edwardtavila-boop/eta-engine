"""APEX PREDATOR  //  strategies.retrospective
====================================================
Trade-outcome record + per-band performance report emitted by the
:class:`RetrospectiveManager` after each closed trade.

A "retrospective" is a structured post-mortem the manager runs every
time a trade closes. It rolls the outcome into the appropriate
(strategy, regime, equity-band) bucket and emits a report when
recent stats cross a policy edge -- e.g. the bucket dipped below the
win-rate floor for N consecutive trades, or recovered above the
reinstate band.

This module owns the data shapes only. The actual rolling logic and
band semantics live in :mod:`strategies.retrospective_wiring`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apex_predator.strategies.adaptive_sizing import RegimeLabel
    from apex_predator.strategies.models import StrategyId


class RetrospectiveVerdict(StrEnum):
    """Policy decision attached to a fired report."""
    CONTINUE = "CONTINUE"
    DEMOTE_TO_PAPER = "DEMOTE_TO_PAPER"
    REINSTATE = "REINSTATE"


@dataclass(frozen=True)
class TradeOutcome:
    """One closed trade as fed into the retrospective manager."""
    strategy: StrategyId
    regime: RegimeLabel
    pnl_r: float
    equity_after: float
    closed_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class BucketStats:
    """Rolling stats for a single (strategy, regime) bucket."""
    n: int
    wins: int
    win_rate: float
    cum_r: float
    expectancy_r: float


@dataclass(frozen=True)
class RetrospectiveReport:
    """Output of :meth:`RetrospectiveManager.record_trade`.

    Always returned -- the verdict tells the caller what to do.
    """
    strategy: StrategyId
    regime: RegimeLabel
    verdict: RetrospectiveVerdict
    stats: BucketStats
    equity_after: float
    note: str = ""


__all__ = [
    "BucketStats",
    "RetrospectiveReport",
    "RetrospectiveVerdict",
    "TradeOutcome",
]
