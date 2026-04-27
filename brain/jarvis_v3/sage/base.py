"""Sage base types: MarketContext, SchoolVerdict, SageReport, SchoolBase.

The MarketContext is the input every school sees. The SchoolVerdict is
each school's atomic output. The SageReport aggregates verdicts.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Bias(StrEnum):
    """Directional bias of a school's verdict."""

    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class MarketContext:
    """Input to every school analyzer.

    ``bars`` is a list of dicts with at least ``open/high/low/close/volume``;
    additional keys like ``ts`` are passed through. We use plain dicts
    rather than a Bar dataclass so the sage works with any data
    pipeline that produces OHLCV (parquet, ccxt, ibkr_bbo1m, etc.).
    """

    bars: list[dict[str, Any]]
    side: str = "long"  # the bot's PROPOSED entry side
    entry_price: float = 0.0
    symbol: str = ""
    # Optional extras a school may use if present
    order_book_imbalance: float | None = None     # -1.0 to +1.0
    cumulative_delta: float | None = None
    realized_vol: float | None = None
    session_phase: str | None = None
    account_equity_usd: float | None = None
    risk_per_trade_pct: float | None = None       # for risk school
    stop_distance_pct: float | None = None        # for risk school

    @property
    def n_bars(self) -> int:
        return len(self.bars)

    def closes(self) -> list[float]:
        return [float(b["close"]) for b in self.bars]

    def highs(self) -> list[float]:
        return [float(b["high"]) for b in self.bars]

    def lows(self) -> list[float]:
        return [float(b["low"]) for b in self.bars]

    def volumes(self) -> list[float]:
        return [float(b.get("volume", 0.0)) for b in self.bars]


@dataclass(frozen=True)
class SchoolVerdict:
    """One school's verdict on the proposed trade.

    ``bias``: directional read (LONG/SHORT/NEUTRAL).
    ``conviction``: 0.0 (no opinion) to 1.0 (high conviction).
    ``aligned_with_entry``: True when bias matches ctx.side.
    ``rationale``: brief (<200 char) text explaining why.
    ``signals``: per-school signals dict for the audit trail
                 (e.g. {"trend": "up", "ma50_above_ma200": True}).
    """

    school: str
    bias: Bias
    conviction: float = 0.0
    aligned_with_entry: bool = False
    rationale: str = ""
    signals: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Pydantic-light validation
        if not 0.0 <= self.conviction <= 1.0:
            raise ValueError(f"conviction must be in [0,1], got {self.conviction}")


@dataclass(frozen=True)
class SageReport:
    """Aggregated verdict across all consulted schools."""

    per_school: dict[str, SchoolVerdict]
    composite_bias: Bias
    conviction: float                     # 0..1
    schools_consulted: int
    schools_aligned_with_entry: int
    schools_disagreeing_with_entry: int
    schools_neutral: int
    rationale: str = ""

    @property
    def consensus_pct(self) -> float:
        """Percentage of schools whose bias matches composite_bias."""
        if not self.per_school:
            return 0.0
        matching = sum(1 for v in self.per_school.values() if v.bias == self.composite_bias)
        return matching / len(self.per_school)

    @property
    def alignment_score(self) -> float:
        """0..1 -- how many schools agree with the proposed entry side.

        1.0 = unanimous; 0.5 = split; 0.0 = unanimous against."""
        n = self.schools_aligned_with_entry + self.schools_disagreeing_with_entry
        if n == 0:
            return 0.5  # all neutral -> we're neutral on alignment
        return self.schools_aligned_with_entry / n

    def summary_line(self) -> str:
        """One-line summary suitable for journal events / Resend bodies."""
        return (
            f"sage: bias={self.composite_bias.value} conv={self.conviction:.2f} "
            f"align={self.alignment_score:.2f} ({self.schools_aligned_with_entry}/"
            f"{self.schools_consulted}) consensus={self.consensus_pct:.2f}"
        )


class SchoolBase(abc.ABC):
    """Abstract base for every market-theory school.

    Each subclass declares NAME + KNOWLEDGE class attributes and
    implements analyze(). Stateless: analyzers must not retain state
    between calls.
    """

    NAME: str = ""
    KNOWLEDGE: str = ""
    WEIGHT: float = 1.0  # confluence aggregator weight

    @abc.abstractmethod
    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        """Return this school's verdict on the proposed trade."""
        ...
