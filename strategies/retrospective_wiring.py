"""APEX PREDATOR  //  strategies.retrospective_wiring
==========================================================
Live retrospective manager. One instance per bot; it rolls each closed
trade into a (strategy, regime) bucket, evaluates demote/reinstate
policy, and pushes the regime+equity tape on every bar so equity-band
analytics can resolve later.

Bucket policy (defaults match the v0.1.48 framework note):

* DEMOTE_TO_PAPER:   trailing window of ``demote_window`` trades has
                     win_rate below ``demote_win_rate_floor`` AND
                     cumulative R below 0.
* REINSTATE:         a previously-demoted bucket has ``reinstate_window``
                     trades since demotion, all clean (win_rate above
                     floor, cum R above 0).
* CONTINUE:          everything else.

The manager is a pure ledger: it doesn't act on the verdict. Callers
(the bot, the supervisor, the dashboard) consume the report and route
it through their own gate. Failures inside ``record_trade`` /
``on_bar`` are caller's responsibility -- they should not crash the
trading loop, which is enforced at the call site.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from apex_predator.strategies.retrospective import (
    BucketStats,
    RetrospectiveReport,
    RetrospectiveVerdict,
)

if TYPE_CHECKING:
    from apex_predator.strategies.adaptive_sizing import RegimeLabel
    from apex_predator.strategies.models import StrategyId
    from apex_predator.strategies.retrospective import TradeOutcome


_DEFAULT_DEMOTE_WINDOW: int = 20
_DEFAULT_DEMOTE_WIN_RATE_FLOOR: float = 0.40
_DEFAULT_REINSTATE_WINDOW: int = 20
_DEFAULT_REINSTATE_WIN_RATE_FLOOR: float = 0.52


@dataclass
class _Bucket:
    trades: deque[float] = field(default_factory=deque)
    wins: int = 0
    cum_r: float = 0.0
    demoted: bool = False
    trades_since_demotion: int = 0
    wins_since_demotion: int = 0
    cum_r_since_demotion: float = 0.0


@dataclass(frozen=True)
class BarTick:
    regime: RegimeLabel
    equity: float


class RetrospectiveManager:
    """One per bot. Rolls trades into per-bucket stats + emits reports."""

    def __init__(
        self,
        *,
        starting_equity: float,
        demote_window: int = _DEFAULT_DEMOTE_WINDOW,
        demote_win_rate_floor: float = _DEFAULT_DEMOTE_WIN_RATE_FLOOR,
        reinstate_window: int = _DEFAULT_REINSTATE_WINDOW,
        reinstate_win_rate_floor: float = _DEFAULT_REINSTATE_WIN_RATE_FLOOR,
    ) -> None:
        if starting_equity <= 0.0:
            raise ValueError("starting_equity must be > 0")
        if demote_window <= 0 or reinstate_window <= 0:
            raise ValueError("windows must be > 0")
        self.starting_equity = starting_equity
        self.demote_window = demote_window
        self.demote_win_rate_floor = demote_win_rate_floor
        self.reinstate_window = reinstate_window
        self.reinstate_win_rate_floor = reinstate_win_rate_floor

        self._buckets: dict[tuple[StrategyId, RegimeLabel], _Bucket] = (
            defaultdict(_Bucket)
        )
        self._latest_bar: BarTick | None = None

    def on_bar(self, *, regime: RegimeLabel, equity: float) -> None:
        """Push the current regime + equity. Cheap. Safe to call every tick."""
        self._latest_bar = BarTick(regime=regime, equity=equity)

    def record_trade(self, outcome: TradeOutcome) -> RetrospectiveReport:
        """Roll a closed trade into its bucket and return a verdict."""
        key = (outcome.strategy, outcome.regime)
        bucket = self._buckets[key]

        is_win = outcome.pnl_r > 0.0
        bucket.trades.append(outcome.pnl_r)
        bucket.cum_r += outcome.pnl_r
        if is_win:
            bucket.wins += 1
        if len(bucket.trades) > self.demote_window:
            evicted = bucket.trades.popleft()
            bucket.cum_r -= evicted
            if evicted > 0.0:
                bucket.wins -= 1

        if bucket.demoted:
            bucket.trades_since_demotion += 1
            bucket.cum_r_since_demotion += outcome.pnl_r
            if is_win:
                bucket.wins_since_demotion += 1

        verdict = self._evaluate(bucket)
        if verdict == RetrospectiveVerdict.DEMOTE_TO_PAPER and not bucket.demoted:
            bucket.demoted = True
            bucket.trades_since_demotion = 0
            bucket.wins_since_demotion = 0
            bucket.cum_r_since_demotion = 0.0
        elif verdict == RetrospectiveVerdict.REINSTATE and bucket.demoted:
            bucket.demoted = False
            bucket.trades_since_demotion = 0
            bucket.wins_since_demotion = 0
            bucket.cum_r_since_demotion = 0.0

        n = len(bucket.trades)
        wr = bucket.wins / n if n > 0 else 0.0
        exp = bucket.cum_r / n if n > 0 else 0.0
        stats = BucketStats(
            n=n,
            wins=bucket.wins,
            win_rate=wr,
            cum_r=bucket.cum_r,
            expectancy_r=exp,
        )
        return RetrospectiveReport(
            strategy=outcome.strategy,
            regime=outcome.regime,
            verdict=verdict,
            stats=stats,
            equity_after=outcome.equity_after,
            note=self._note_for(verdict, stats),
        )

    def stats_for(
        self,
        strategy: StrategyId,
        regime: RegimeLabel,
    ) -> BucketStats | None:
        bucket = self._buckets.get((strategy, regime))
        if bucket is None or not bucket.trades:
            return None
        n = len(bucket.trades)
        return BucketStats(
            n=n,
            wins=bucket.wins,
            win_rate=bucket.wins / n,
            cum_r=bucket.cum_r,
            expectancy_r=bucket.cum_r / n,
        )

    def is_demoted(self, strategy: StrategyId, regime: RegimeLabel) -> bool:
        bucket = self._buckets.get((strategy, regime))
        return bool(bucket and bucket.demoted)

    def _evaluate(self, bucket: _Bucket) -> RetrospectiveVerdict:
        if bucket.demoted:
            if bucket.trades_since_demotion >= self.reinstate_window:
                wr = bucket.wins_since_demotion / bucket.trades_since_demotion
                if (
                    wr >= self.reinstate_win_rate_floor
                    and bucket.cum_r_since_demotion > 0.0
                ):
                    return RetrospectiveVerdict.REINSTATE
            return RetrospectiveVerdict.CONTINUE

        if len(bucket.trades) >= self.demote_window:
            wr = bucket.wins / len(bucket.trades)
            if wr < self.demote_win_rate_floor and bucket.cum_r < 0.0:
                return RetrospectiveVerdict.DEMOTE_TO_PAPER

        return RetrospectiveVerdict.CONTINUE

    def _note_for(self, verdict: RetrospectiveVerdict, stats: BucketStats) -> str:
        if verdict == RetrospectiveVerdict.DEMOTE_TO_PAPER:
            return (
                f"demoted: window={stats.n} win_rate={stats.win_rate:.2f} "
                f"cum_r={stats.cum_r:.2f}"
            )
        if verdict == RetrospectiveVerdict.REINSTATE:
            return (
                f"reinstated: clean window of {self.reinstate_window} trades "
                f"win_rate={stats.win_rate:.2f}"
            )
        return ""


__all__ = [
    "RetrospectiveManager",
]
