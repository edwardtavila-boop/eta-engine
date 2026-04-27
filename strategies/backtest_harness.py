"""EVOLUTIONARY TRADING ALGO  //  strategies.backtest_harness.

Walk-forward-ready replay harness for the six AI-Optimized strategies.

Takes a cold list of :class:`Bar` objects, feeds them one-by-one into
:func:`policy_router.dispatch` with a caller-supplied context, and
records each hypothetical trade (entry, stop, target, exit, R-multiple).
Per-strategy and portfolio-level stats are aggregated at the end so the
caller can calibrate per-asset eligibility thresholds BEFORE any live
capital is routed through the adapter.

Unlike :mod:`backtest.engine`, this harness does not depend on the
pandas-heavy feature pipeline or the full ``BarData`` schema. It works
purely on the frozen :class:`Bar` dataclass that the strategies package
already consumes, which keeps the hot loop allocation-free and makes
the harness trivial to run inside a walk-forward driver.

Design
------
* **Pure replay.** The harness holds only the bar buffer plus open
  trades; everything else (context, registry, eligibility) is injected
  per call. That lets a walk-forward driver snapshot config and swap
  windows without re-allocating state.
* **Trade-by-trade exits.** A trade enters on the close of the bar
  whose dispatch produced an actionable winning signal. Subsequent
  bars' highs/lows are checked against stop/target; first touch wins.
  If neither is hit within ``max_bars_per_trade`` the trade closes at
  the close of the timeout bar (``TIMEOUT`` exit reason).
* **One trade at a time per strategy.** A strategy cannot stack
  positions on itself. Different strategies can run concurrently.
* **Slippage budget.** Optional per-side slippage (bps of entry) is
  subtracted from R to reflect the routing layer's fill distance.

The harness intentionally avoids a bunch of realism knobs (funding,
borrow, partial fills, venue queueing). Those belong in
:mod:`backtest.engine`. The harness's job is to answer *"does the
strategy edge exist on this tape at all?"* — the cheapest first
question to ask when vetting a new AI-Optimized combination.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from eta_engine.strategies.eta_policy import StrategyContext
from eta_engine.strategies.models import (
    Bar,
    Side,
    StrategyId,
    StrategySignal,
)
from eta_engine.strategies.policy_router import (
    DEFAULT_ELIGIBILITY,
    RouterDecision,
    dispatch,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "BacktestReport",
    "ExitReason",
    "HarnessConfig",
    "StrategyBacktestStats",
    "StrategyTrade",
    "default_ctx_builder",
    "run_harness",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ExitReason(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True, slots=True)
class StrategyTrade:
    """One hypothetical round-trip, independent of venue/fees."""

    strategy: StrategyId
    side: Side
    entry_ts: int
    entry: float
    stop: float
    target: float
    exit_ts: int
    exit: float
    exit_reason: ExitReason
    r_multiple: float
    bars_held: int

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "side": self.side.value,
            "entry_ts": self.entry_ts,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "exit_ts": self.exit_ts,
            "exit": self.exit,
            "exit_reason": self.exit_reason.value,
            "r_multiple": self.r_multiple,
            "bars_held": self.bars_held,
        }


@dataclass(frozen=True, slots=True)
class StrategyBacktestStats:
    """Per-strategy aggregated performance."""

    strategy: StrategyId
    n_trades: int
    hit_rate: float
    avg_r: float
    total_r: float
    max_consecutive_losses: int
    longest_trade_bars: int
    avg_trade_bars: float

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy.value,
            "n_trades": self.n_trades,
            "hit_rate": round(self.hit_rate, 4),
            "avg_r": round(self.avg_r, 4),
            "total_r": round(self.total_r, 4),
            "max_consecutive_losses": self.max_consecutive_losses,
            "longest_trade_bars": self.longest_trade_bars,
            "avg_trade_bars": round(self.avg_trade_bars, 2),
        }


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Harness output: trades, per-strategy stats, decision timeline."""

    asset: str
    total_bars: int
    total_trades: int
    trades: tuple[StrategyTrade, ...]
    stats_by_strategy: tuple[StrategyBacktestStats, ...]
    decisions: tuple[RouterDecision, ...] = field(default_factory=tuple)

    @property
    def total_r(self) -> float:
        return round(sum(t.r_multiple for t in self.trades), 4)

    @property
    def hit_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.r_multiple > 0.0)
        return round(wins / len(self.trades), 4)

    def as_dict(self) -> dict[str, object]:
        return {
            "asset": self.asset,
            "total_bars": self.total_bars,
            "total_trades": self.total_trades,
            "total_r": self.total_r,
            "hit_rate": self.hit_rate,
            "stats_by_strategy": [s.as_dict() for s in self.stats_by_strategy],
            # decisions are heavy -- caller opts in when needed
        }


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """Harness runtime knobs.

    * ``warmup_bars`` -- index below which no trades are taken. Must be
      at least as wide as the longest-lookback strategy (MTF trend uses
      a 200-period MA).
    * ``max_bars_per_trade`` -- hard timeout. Prevents a bad stop from
      holding a trade forever if price drifts sideways.
    * ``slippage_bps`` -- flat slippage on entry AND exit, subtracted
      from R. 5 bps is a reasonable default for liquid perps.
    * ``record_decisions`` -- set to True to keep every RouterDecision
      in the report (heavy). Default False for portfolio backtests.
    """

    warmup_bars: int = 200
    max_bars_per_trade: int = 48
    slippage_bps: float = 5.0
    record_decisions: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def default_ctx_builder(_bar: Bar) -> StrategyContext:
    """Default context used when the caller does not supply one.

    Emits the most permissive context possible (TREND regime, mid-5
    confluence, no bias, no kill-switch) so the harness can still tick
    even on asset tapes we don't have a features pipeline for yet. Real
    walk-forward callers should always inject their own builder.
    """
    return StrategyContext(
        regime_label="TREND",
        confluence_score=5.0,
        vol_z=0.0,
        trend_bias=Side.FLAT,
        session_allows_entries=True,
        kill_switch_active=False,
        htf_bias=Side.FLAT,
    )


def _apply_slippage(r: float, slippage_bps: float) -> float:
    """Reduce R by 2 * slippage_bps (entry + exit) expressed in R units.

    This is a coarse but defensible approximation: we assume a 1R stop
    is roughly the same order of magnitude as the entry's bps band, so
    ``2 * bps / 10000`` maps to fractional R. For tight stops the bias
    is conservative (over-penalises).
    """
    penalty_r = 2.0 * slippage_bps / 10_000.0
    return r - penalty_r


def _exit_trade(
    open_trade: _OpenTrade,
    bars: list[Bar],
    cfg: HarnessConfig,
) -> StrategyTrade:
    """Walk forward from open_trade.entry_idx and resolve stop/target/timeout."""
    entry = open_trade.entry
    stop = open_trade.stop
    target = open_trade.target
    side = open_trade.side
    stop_dist = abs(entry - stop)
    # One-R move in the direction of the trade
    # Long: price up = win.  Short: price down = win.
    last_idx = min(open_trade.entry_idx + cfg.max_bars_per_trade, len(bars) - 1)
    for i in range(open_trade.entry_idx + 1, last_idx + 1):
        bar = bars[i]
        if side is Side.LONG:
            # Stop first (pessimistic — same-bar resolution goes against us)
            if bar.low <= stop:
                exit_price = stop
                r = -1.0
                reason = ExitReason.STOP
                return _finalise(open_trade, i, bar, exit_price, r, reason, cfg)
            if bar.high >= target:
                exit_price = target
                r = (target - entry) / stop_dist if stop_dist > 0 else 0.0
                reason = ExitReason.TARGET
                return _finalise(open_trade, i, bar, exit_price, r, reason, cfg)
        else:  # SHORT
            if bar.high >= stop:
                exit_price = stop
                r = -1.0
                reason = ExitReason.STOP
                return _finalise(open_trade, i, bar, exit_price, r, reason, cfg)
            if bar.low <= target:
                exit_price = target
                r = (entry - target) / stop_dist if stop_dist > 0 else 0.0
                reason = ExitReason.TARGET
                return _finalise(open_trade, i, bar, exit_price, r, reason, cfg)
    # Timeout -- close at the last bar's close
    final_bar = bars[last_idx]
    if side is Side.LONG:
        r = (final_bar.close - entry) / stop_dist if stop_dist > 0 else 0.0
    else:
        r = (entry - final_bar.close) / stop_dist if stop_dist > 0 else 0.0
    return _finalise(open_trade, last_idx, final_bar, final_bar.close, r, ExitReason.TIMEOUT, cfg)


def _finalise(
    ot: _OpenTrade,
    exit_idx: int,
    exit_bar: Bar,
    exit_price: float,
    raw_r: float,
    reason: ExitReason,
    cfg: HarnessConfig,
) -> StrategyTrade:
    r_after_slip = _apply_slippage(raw_r, cfg.slippage_bps)
    return StrategyTrade(
        strategy=ot.strategy,
        side=ot.side,
        entry_ts=ot.entry_ts,
        entry=ot.entry,
        stop=ot.stop,
        target=ot.target,
        exit_ts=exit_bar.ts,
        exit=exit_price,
        exit_reason=reason,
        r_multiple=round(r_after_slip, 4),
        bars_held=exit_idx - ot.entry_idx,
    )


@dataclass
class _OpenTrade:
    strategy: StrategyId
    side: Side
    entry_idx: int
    entry_ts: int
    entry: float
    stop: float
    target: float


def _aggregate_stats(
    trades: list[StrategyTrade],
    strategies_seen: set[StrategyId],
) -> list[StrategyBacktestStats]:
    out: list[StrategyBacktestStats] = []
    for sid in sorted(strategies_seen, key=lambda s: s.value):
        rows = [t for t in trades if t.strategy is sid]
        if not rows:
            continue
        n = len(rows)
        wins = sum(1 for t in rows if t.r_multiple > 0)
        total_r = sum(t.r_multiple for t in rows)
        bar_counts = [t.bars_held for t in rows]
        max_consec_losses = 0
        running = 0
        for t in rows:
            if t.r_multiple < 0:
                running += 1
                max_consec_losses = max(max_consec_losses, running)
            else:
                running = 0
        out.append(
            StrategyBacktestStats(
                strategy=sid,
                n_trades=n,
                hit_rate=wins / n,
                avg_r=total_r / n,
                total_r=total_r,
                max_consecutive_losses=max_consec_losses,
                longest_trade_bars=max(bar_counts),
                avg_trade_bars=sum(bar_counts) / n,
            ),
        )
    return out


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def run_harness(
    bars: list[Bar],
    asset: str,
    *,
    ctx_builder: Callable[[Bar], StrategyContext] | None = None,
    config: HarnessConfig | None = None,
    eligibility: dict[str, tuple[StrategyId, ...]] | None = None,
    registry: dict[StrategyId, Callable[..., StrategySignal]] | None = None,
) -> BacktestReport:
    """Replay ``bars`` through :func:`policy_router.dispatch` and score trades.

    Parameters
    ----------
    bars:
        Oldest-first list of :class:`Bar`. Must be monotonic in ``ts``.
    asset:
        Symbol ticker used for eligibility lookup (e.g. ``"MNQ"``).
    ctx_builder:
        Per-bar callable returning a :class:`StrategyContext`. Defaults
        to :func:`default_ctx_builder` for cold runs.
    config:
        :class:`HarnessConfig` knobs. Defaults to permissive settings.
    eligibility, registry:
        Forwarded to :func:`dispatch` for test injection.

    Returns
    -------
    BacktestReport
        Per-strategy stats + the full trade tape.
    """
    cfg = config or HarnessConfig()
    builder = ctx_builder or default_ctx_builder
    table = eligibility if eligibility is not None else DEFAULT_ELIGIBILITY

    if len(bars) < cfg.warmup_bars + 2:
        return BacktestReport(
            asset=asset.upper(),
            total_bars=len(bars),
            total_trades=0,
            trades=(),
            stats_by_strategy=(),
            decisions=(),
        )

    trades: list[StrategyTrade] = []
    open_per_strategy: dict[StrategyId, _OpenTrade] = {}
    decisions: list[RouterDecision] = []
    strategies_seen: set[StrategyId] = set()

    for i in range(cfg.warmup_bars, len(bars)):
        window = bars[: i + 1]
        current = bars[i]
        ctx = builder(current)
        decision = dispatch(
            asset,
            window,
            ctx,
            eligibility=table,
            registry=registry,
        )
        if cfg.record_decisions:
            decisions.append(decision)

        winner = decision.winner
        if not winner.is_actionable:
            continue
        # One trade per strategy at a time — if already open, skip.
        if winner.strategy in open_per_strategy:
            continue
        # Need a valid stop to compute R.
        if winner.stop <= 0.0 or winner.entry <= 0.0:
            continue
        # Skip degenerate 0-distance signals.
        if abs(winner.entry - winner.stop) <= 0.0:
            continue

        ot = _OpenTrade(
            strategy=winner.strategy,
            side=winner.side,
            entry_idx=i,
            entry_ts=current.ts,
            entry=winner.entry,
            stop=winner.stop,
            target=(winner.target if winner.target > 0.0 else _fallback_target(winner)),
        )
        open_per_strategy[winner.strategy] = ot
        strategies_seen.add(winner.strategy)

        # Resolve the trade immediately so the report carries full round-trips.
        finished = _exit_trade(ot, bars, cfg)
        trades.append(finished)
        # Close the slot so future bars can open a new trade for this strategy.
        del open_per_strategy[winner.strategy]

    stats = _aggregate_stats(trades, strategies_seen)
    return BacktestReport(
        asset=asset.upper(),
        total_bars=len(bars),
        total_trades=len(trades),
        trades=tuple(trades),
        stats_by_strategy=tuple(stats),
        decisions=tuple(decisions),
    )


def _fallback_target(signal: StrategySignal) -> float:
    """Construct a 2R target when a strategy did not provide one.

    Most detectors supply a target, but ``rl_full_automation`` and a
    couple of regime variants sometimes return signals without an
    explicit target (the RL layer's downstream manager sets it live).
    For a backtest we need SOMETHING -- 2R is the founder-brief default.
    """
    stop_distance = abs(signal.entry - signal.stop)
    if signal.side is Side.LONG:
        return signal.entry + 2.0 * stop_distance
    return signal.entry - 2.0 * stop_distance
