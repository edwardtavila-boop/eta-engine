"""EVOLUTIONARY TRADING ALGO  //  strategies.models.

Shared value objects for the strategies package.

All models are frozen dataclasses so they can flow safely across async
task boundaries (no hidden mutation) and be held inside the
``JarvisContext`` / decision-journal payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Side(StrEnum):
    """Trade side. ``FLAT`` means no trade -- the detector abstained."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class StrategyId(StrEnum):
    """Named strategies from the founder brief.

    Order = ranking by ``edge * ease-of-automation`` as delivered in
    the recommended-strategies note.
    """

    LIQUIDITY_SWEEP_DISPLACEMENT = "liquidity_sweep_displacement"
    OB_BREAKER_RETEST = "ob_breaker_retest"
    FVG_FILL_CONFLUENCE = "fvg_fill_confluence"
    MTF_TREND_FOLLOWING = "mtf_trend_following"
    REGIME_ADAPTIVE_ALLOCATION = "regime_adaptive_allocation"
    RL_FULL_AUTOMATION = "rl_full_automation"


# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """Minimal OHLCV bar.

    ``ts`` is a monotonic integer (epoch millis is fine); the primitives
    never reason about wall-clock time.
    """

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bull(self) -> bool:
        return self.close > self.open

    @property
    def is_bear(self) -> bool:
        return self.close < self.open


# ---------------------------------------------------------------------------
# StrategySignal
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategySignal:
    """Per-strategy decision.

    ``confidence`` is on the same 0..10 scale as the existing
    :mod:`eta_engine.core.confluence_scorer` so downstream sizing can
    merge it with the venue-agnostic confluence score.

    ``risk_mult`` is a *multiplicative* modifier on the bot's base
    ``risk_per_trade_pct``. Typical range: [0.25, 1.5]. Values <= 0.0
    abstain -- the signal was produced but not actionable.

    ``rationale_tags`` are short string fragments listing which
    primitives fired. Picked up by the rationale-miner for post-hoc
    pattern attribution.
    """

    strategy: StrategyId
    side: Side
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    confidence: float = 0.0  # 0..10
    risk_mult: float = 0.0  # base-risk modifier
    rationale_tags: tuple[str, ...] = field(default_factory=tuple)
    meta: dict[str, float] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.side is not Side.FLAT and self.confidence > 0.0 and self.risk_mult > 0.0

    @property
    def rr(self) -> float:
        """Reward-to-risk ratio. Zero if stop or target is unset."""
        risk = abs(self.entry - self.stop)
        reward = abs(self.target - self.entry)
        if risk <= 0.0:
            return 0.0
        return reward / risk

    def as_dict(self) -> dict[str, float | str | list[str] | dict[str, float]]:
        """JSON-safe dict for decision-journal + admin-audit payloads."""
        return {
            "strategy": self.strategy.value,
            "side": self.side.value,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "confidence": self.confidence,
            "risk_mult": self.risk_mult,
            "rr": self.rr,
            "rationale_tags": list(self.rationale_tags),
            "meta": dict(self.meta),
        }


FLAT_SIGNAL = StrategySignal(
    strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
    side=Side.FLAT,
)
"""Sentinel for "no setup this bar"."""
