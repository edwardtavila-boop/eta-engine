"""Sage base types: MarketContext, SchoolVerdict, SageReport, SchoolBase.

The MarketContext is the input every school sees. The SchoolVerdict is
each school's atomic output. The SageReport aggregates verdicts.
"""

from __future__ import annotations

import abc
import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Bias(StrEnum):
    """Directional bias of a school's verdict."""

    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


_SENTINEL = object()


@dataclass(frozen=True)
class MarketContext:
    """Input to every school analyzer.

    ``bars`` is the PRIMARY-timeframe bar list (back-compat: many schools
    only consult one TF). For multi-timeframe schools, populate
    ``bars_by_tf`` with a dict like ``{"1m": [...], "5m": [...], "1h": [...]}``.

    Bars are plain dicts with at least ``open/high/low/close/volume``;
    additional keys like ``ts`` are passed through. We use plain dicts
    rather than a Bar dataclass so the sage works with any data
    pipeline (parquet, ccxt, ibkr_bbo1m, etc.).
    """

    bars: list[dict[str, Any]]
    side: str = "long"  # the bot's PROPOSED entry side
    entry_price: float = 0.0
    symbol: str = ""
    bars_by_tf: dict[str, list[dict[str, Any]]] | None = None
    order_book_imbalance: float | None = None  # -1.0 to +1.0
    cumulative_delta: float | None = None
    realized_vol: float | None = None
    session_phase: str | None = None
    account_equity_usd: float | None = None
    risk_per_trade_pct: float | None = None  # for risk school
    stop_distance_pct: float | None = None  # for risk school
    detected_regime: str | None = None  # one of {trending, ranging, volatile, quiet}
    instrument_class: str | None = None  # one of {equity, crypto, futures, fx, options}
    onchain: dict[str, Any] | None = None  # for OnChainSchool (BTC/ETH metrics)
    funding: dict[str, Any] | None = None  # for FundingBasisSchool (perp funding + basis)
    options: dict[str, Any] | None = None  # for OptionsGreeksSchool (IV / skew / GEX)
    peer_returns: dict[str, list[float]] | None = None  # for CrossAssetCorrelationSchool
    _cached: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def n_bars(self) -> int:
        return len(self.bars)

    def closes(self) -> list[float]:
        cached = self._cached.get("closes", _SENTINEL)
        if cached is not _SENTINEL:
            return cached
        result = [float(b["close"]) for b in self.bars]
        self._cached["closes"] = result
        return result

    def highs(self) -> list[float]:
        cached = self._cached.get("highs", _SENTINEL)
        if cached is not _SENTINEL:
            return cached
        result = [float(b["high"]) for b in self.bars]
        self._cached["highs"] = result
        return result

    def lows(self) -> list[float]:
        cached = self._cached.get("lows", _SENTINEL)
        if cached is not _SENTINEL:
            return cached
        result = [float(b["low"]) for b in self.bars]
        self._cached["lows"] = result
        return result

    def volumes(self) -> list[float]:
        cached = self._cached.get("volumes", _SENTINEL)
        if cached is not _SENTINEL:
            return cached
        result = [float(b.get("volume", 0.0)) for b in self.bars]
        self._cached["volumes"] = result
        return result

    def has_tf(self, tf: str) -> bool:
        """True if `bars_by_tf` contains the given timeframe label."""
        return self.bars_by_tf is not None and tf in self.bars_by_tf

    def for_tf(self, tf: str) -> MarketContext:
        """Return a new MarketContext rebound to the bars at `tf`.

        Preserves all other fields. Useful for schools that need to
        re-run their own analyze() against a different timeframe.
        """
        if not self.has_tf(tf):
            return self
        new_bars = self.bars_by_tf[tf]  # type: ignore[index]
        return MarketContext(
            bars=new_bars,
            side=self.side,
            entry_price=self.entry_price,
            symbol=self.symbol,
            bars_by_tf=self.bars_by_tf,
            order_book_imbalance=self.order_book_imbalance,
            cumulative_delta=self.cumulative_delta,
            realized_vol=self.realized_vol,
            session_phase=self.session_phase,
            account_equity_usd=self.account_equity_usd,
            risk_per_trade_pct=self.risk_per_trade_pct,
            stop_distance_pct=self.stop_distance_pct,
            detected_regime=self.detected_regime,
            instrument_class=self.instrument_class,
            onchain=self.onchain,
            funding=self.funding,
            options=self.options,
            peer_returns=self.peer_returns,
        )

    def with_regime(self, regime: str) -> MarketContext:
        """Return a new MarketContext with ``detected_regime`` set.

        Avoids rebuilding all fields manually -- uses shallow copy."""
        ctx = copy.copy(self)
        object.__setattr__(ctx, "detected_regime", regime)
        return ctx


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
    conviction: float  # 0..1
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
    WEIGHT: float = 1.0  # confluence aggregator weight (default; learned weights override at runtime)

    # Wave-5 #6 (per-instrument activation): which instrument classes
    # this school is meaningfully designed for. Empty set = applies to all.
    INSTRUMENTS: frozenset[str] = frozenset()  # e.g. {"equity", "futures"}

    # Wave-5 #1 (multi-timeframe): set True when the school opts into
    # consulting multiple timeframes from MarketContext.bars_by_tf.
    MULTI_TIMEFRAME: bool = False

    # Wave-5 #2 (regime gates): regimes in which this school is
    # MEANINGFULLY useful. Empty set = applies in all regimes.
    REGIMES: frozenset[str] = frozenset()  # e.g. {"trending"}

    def applies_to(self, ctx: MarketContext) -> bool:
        """True when this school is enabled for the given context.

        Off by default if INSTRUMENTS specified + ctx.instrument_class
        not in the set, OR REGIMES specified + ctx.detected_regime not
        in the set. Schools with empty INSTRUMENTS+REGIMES apply
        universally (back-compat with the original 14 schools).
        """
        if self.INSTRUMENTS and ctx.instrument_class is not None and ctx.instrument_class not in self.INSTRUMENTS:
            return False
        return not (self.REGIMES and ctx.detected_regime is not None and ctx.detected_regime not in self.REGIMES)

    @abc.abstractmethod
    def analyze(self, ctx: MarketContext) -> SchoolVerdict:
        """Return this school's verdict on the proposed trade."""
        ...
