"""EVOLUTIONARY TRADING ALGO  //  strategies.policy_router.

Per-asset dispatch for the six named strategies.

The router owns two decisions:

  1. Which strategies are eligible for a given asset (e.g. MNQ runs
     #1 + #4 per the founder brief; crypto runs #1-#4 + #6).
  2. Which candidate signal wins when multiple strategies fire.

Both are pure functions of the inputs -- no hidden state, no I/O --
so they can be called from inside the existing bot
:meth:`on_bar` handlers or from the live supervisor tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from eta_engine.strategies.eta_policy import (
    STRATEGIES,
    StrategyContext,
)
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Per-asset eligibility
# ---------------------------------------------------------------------------


DEFAULT_ELIGIBILITY: dict[str, tuple[StrategyId, ...]] = {
    "MNQ": (
        StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
        StrategyId.OB_BREAKER_RETEST,
        StrategyId.FVG_FILL_CONFLUENCE,
        StrategyId.MTF_TREND_FOLLOWING,
    ),
    "NQ": (
        StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
        StrategyId.OB_BREAKER_RETEST,
        StrategyId.MTF_TREND_FOLLOWING,
    ),
    "BTC": (
        StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
        StrategyId.OB_BREAKER_RETEST,
        StrategyId.FVG_FILL_CONFLUENCE,
        StrategyId.MTF_TREND_FOLLOWING,
        StrategyId.RL_FULL_AUTOMATION,
    ),
    "ETH": (
        StrategyId.OB_BREAKER_RETEST,
        StrategyId.FVG_FILL_CONFLUENCE,
        StrategyId.MTF_TREND_FOLLOWING,
    ),
    "SOL": (
        StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
        StrategyId.FVG_FILL_CONFLUENCE,
        StrategyId.RL_FULL_AUTOMATION,
    ),
    "XRP": (
        StrategyId.FVG_FILL_CONFLUENCE,
        StrategyId.MTF_TREND_FOLLOWING,
    ),
    "PORTFOLIO": (StrategyId.REGIME_ADAPTIVE_ALLOCATION,),
}
"""Asset symbol -> eligible strategies, in ranking order."""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouterDecision:
    """Router output: the winning signal plus all candidates for audit."""

    asset: str
    winner: StrategySignal
    candidates: tuple[StrategySignal, ...]
    eligible: tuple[StrategyId, ...] = field(default_factory=tuple)

    @property
    def fired_count(self) -> int:
        return sum(1 for c in self.candidates if c.is_actionable)

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "winner": self.winner.as_dict(),
            "candidates": [c.as_dict() for c in self.candidates],
            "eligible": [e.value for e in self.eligible],
            "fired_count": self.fired_count,
        }


def _score(signal: StrategySignal) -> float:
    """Ranking score for candidate selection.

    Confidence dominates; risk_mult is a tiebreaker. Non-actionable
    signals score zero so they can never win.
    """
    if not signal.is_actionable:
        return 0.0
    return signal.confidence + signal.risk_mult * 0.1


def dispatch(
    asset: str,
    bars: list[Bar],
    ctx: StrategyContext,
    *,
    eligibility: dict[str, tuple[StrategyId, ...]] | None = None,
    registry: dict[StrategyId, Callable[..., StrategySignal]] | None = None,
) -> RouterDecision:
    """Run every eligible strategy for ``asset`` and pick the winner.

    Parameters
    ----------
    asset:
        Symbol ticker, e.g. ``"MNQ"`` or ``"BTC"``. Unknown symbols fall
        back to the first four strategies (ambushes + FVG + MTF trend).
    bars:
        Oldest-first list of bars.
    ctx:
        Shared :class:`StrategyContext`.
    eligibility, registry:
        Optional injection points for tests.
    """
    table = eligibility if eligibility is not None else DEFAULT_ELIGIBILITY
    reg = registry if registry is not None else STRATEGIES

    eligible = table.get(
        asset.upper(),
        (
            StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            StrategyId.OB_BREAKER_RETEST,
            StrategyId.FVG_FILL_CONFLUENCE,
            StrategyId.MTF_TREND_FOLLOWING,
        ),
    )

    candidates: list[StrategySignal] = []
    for sid in eligible:
        fn = reg.get(sid)
        if fn is None:
            continue
        candidates.append(fn(bars, ctx))

    best = max(
        candidates,
        key=_score,
        default=StrategySignal(
            strategy=StrategyId.LIQUIDITY_SWEEP_DISPLACEMENT,
            side=Side.FLAT,
            rationale_tags=("no_candidates",),
        ),
    )
    return RouterDecision(
        asset=asset.upper(),
        winner=best,
        candidates=tuple(candidates),
        eligible=tuple(eligible),
    )
