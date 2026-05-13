"""
EVOLUTIONARY TRADING ALGO  //  strategies.multi_strategy_composite
====================================================================
Multi-strategy parallel composite — one bot runs N strategies.

User mandate (2026-04-27): "one bot can run multiple strategies".

Mechanic
--------
On each bar, every sub-strategy gets ``maybe_enter()`` called so
its internal state advances (EMAs, cooldowns, day counters). When
multiple sub-strategies propose entries on the same bar, the
composite picks ONE according to a configurable policy.

This is DIFFERENT from ``EnsembleVotingStrategy`` (which requires
N strategies to AGREE before firing) — here, each strategy fires
INDEPENDENTLY, and the composite just arbitrates capital
allocation when there's a conflict.

Conflict resolution policies
----------------------------
* ``priority``: take entry from the highest-priority sub (the
  first that fires). Other proposals are dropped this bar.
* ``confluence_weighted``: take the proposal with the highest
  ``opened.confluence`` score. Ties broken by priority order.
* ``best_rr``: take the proposal with the highest implied R-R
  (i.e. (target - entry) / (entry - stop)).

The composite tracks per-strategy fire counts for audit + later
capital-allocation tuning.

Trade attribution
-----------------
The engine's ``on_trade_close`` callback fires once per realized
trade. The composite forwards the callback to the ORIGINATING
sub-strategy (whichever fired the entry). This way:
* AdaptiveKellySizing on sub-strategy A only sees A's outcomes
* Sub-strategy B's R-streak ledger isn't polluted by A's outcomes

This requires the composite to remember which sub fired the
currently-open trade. We track that via a bar-keyed handle.

Use cases the composite enables
-------------------------------
1. **MNQ multi-strategy bot**: 5m ORB + 15m+1m scalper running
   in parallel, doubling trade-count on choppy days where both
   fire, fall back to either when one is dormant.

2. **BTC multi-strategy bot**: +6.00 sage_daily_etf champion +
   funding-divergence as a contrarian counter-trend; the funding
   strategy fires when sage sits, vice versa.

3. **Cross-symbol portfolio**: not handled here (that's a
   bot-portfolio level concern). This composite is bar-stream
   level — same symbol, parallel mechanics.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Protocol

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig, Trade
    from eta_engine.core.data_pipeline import BarData

    class _SubStrategy(Protocol):
        def maybe_enter(
            self,
            bar: BarData,
            hist: list[BarData],
            equity: float,
            config: BacktestConfig,
        ) -> _Open | None: ...


@dataclass(frozen=True)
class MultiStrategyConfig:
    """Knobs for the parallel composite."""

    # Conflict-resolution policy when N strategies fire on the
    # same bar: 'priority', 'confluence_weighted', 'best_rr'.
    conflict_policy: str = "priority"

    # Tag the trade's ``regime`` field with the originating
    # sub-strategy's index. Useful for post-mortem attribution.
    tag_originator: bool = True


@dataclass
class _SubFireRecord:
    """Audit row for a single sub-strategy's per-bar fire stats."""

    fired: int = 0
    selected: int = 0  # times this sub's proposal won the conflict
    callbacks_received: int = 0


class MultiStrategyComposite:
    """Run N sub-strategies in parallel; arbitrate when they conflict.

    Construction takes a list of (name, strategy) tuples. Order
    matters when ``conflict_policy='priority'`` — earlier subs win
    ties.
    """

    def __init__(
        self,
        sub_strategies: list[tuple[str, object]],
        config: MultiStrategyConfig | None = None,
    ) -> None:
        if not sub_strategies:
            raise ValueError("MultiStrategyComposite needs at least 1 sub")
        self._subs: list[tuple[str, object]] = list(sub_strategies)
        self.cfg = config or MultiStrategyConfig()
        # Audit: per-sub fire records
        self._records: dict[str, _SubFireRecord] = {name: _SubFireRecord() for name, _ in self._subs}
        # Track which sub originated the currently-open trade so we
        # can route on_trade_close to the right listener.
        self._current_originator: str | None = None

    # -- audit -------------------------------------------------------------

    @property
    def fire_records(self) -> dict[str, dict[str, int]]:
        return {
            name: {
                "fired": rec.fired,
                "selected": rec.selected,
                "callbacks_received": rec.callbacks_received,
            }
            for name, rec in self._records.items()
        }

    @property
    def sub_names(self) -> list[str]:
        return [name for name, _ in self._subs]

    # -- callback routing --------------------------------------------------

    def on_trade_close(self, trade: Trade) -> None:
        """Engine callback. Routes to the originator sub-strategy
        if that sub has its own ``on_trade_close``."""
        origin = self._current_originator
        if origin is not None:
            self._records[origin].callbacks_received += 1
            sub = next((s for n, s in self._subs if n == origin), None)
            cb = getattr(sub, "on_trade_close", None) if sub else None
            if cb is not None:
                import contextlib

                with contextlib.suppress(Exception):
                    cb(trade)
        # The composite doesn't track open positions itself; the
        # engine fires this callback once per close, so we clear
        # the originator handle. If the engine fires another open
        # in a later bar, the next maybe_enter() repopulates it.
        self._current_originator = None

    # -- conflict resolution -----------------------------------------------

    def _select_winner(
        self,
        proposals: list[tuple[str, object, _Open]],
    ) -> tuple[str, _Open]:
        """Pick the winning proposal based on configured policy.

        Pre: proposals is non-empty. Returns (name, _Open).
        """
        if len(proposals) == 1:
            name, _, opened = proposals[0]
            return name, opened

        policy = self.cfg.conflict_policy
        if policy == "priority":
            # First in declaration order wins (preserves caller intent)
            name, _, opened = proposals[0]
            return name, opened
        if policy == "confluence_weighted":
            # Highest confluence score wins; ties → first-in-list
            best = max(proposals, key=lambda p: p[2].confluence)
            return best[0], best[2]
        if policy == "best_rr":
            # Highest |target - entry| / |entry - stop| wins
            def _rr(o: _Open) -> float:
                stop_dist = abs(o.entry_price - o.stop) or 1e-9
                tgt_dist = abs(o.target - o.entry_price)
                return tgt_dist / stop_dist

            best = max(proposals, key=lambda p: _rr(p[2]))
            return best[0], best[2]
        # Unknown policy → fall back to priority
        name, _, opened = proposals[0]
        return name, opened

    # -- main entry point --------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Always advance EVERY sub's state — even if one of them
        # ends up "winning" the conflict, the others should evolve
        # their EMAs / cooldowns for the next bar.
        proposals: list[tuple[str, object, _Open]] = []
        for name, sub in self._subs:
            try:
                opened = sub.maybe_enter(bar, hist, equity, config)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - sub isolation
                opened = None
            if opened is not None:
                self._records[name].fired += 1
                proposals.append((name, sub, opened))

        if not proposals:
            return None

        winner_name, winner_open = self._select_winner(proposals)
        self._records[winner_name].selected += 1
        self._current_originator = winner_name

        if not self.cfg.tag_originator:
            return winner_open
        # Tag the trade so post-mortem audit can attribute by
        # originating sub-strategy.
        new_tag = f"{winner_open.regime}_origin_{winner_name}"
        return replace(winner_open, regime=new_tag)
