"""
EVOLUTIONARY TRADING ALGO  //  backtest.engine
===================================
Bar-by-bar backtest runner. Pure Python — no pandas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eta_engine.backtest.metrics import (
    compute_expectancy,
    compute_max_dd,
    compute_profit_factor,
    compute_sharpe,
    compute_sortino,
)
from eta_engine.backtest.models import BacktestConfig, BacktestResult, Trade
from eta_engine.core.confluence_scorer import ConfluenceResult, score_confluence

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from eta_engine.core.data_pipeline import BarData
    from eta_engine.features.pipeline import FeaturePipeline


@dataclass
class _Open:
    entry_bar: BarData
    side: str
    qty: float
    entry_price: float
    stop: float
    target: float
    risk_usd: float
    confluence: float
    leverage: float
    peak_adverse: float = 0.0
    # Regime label active at trade entry; lifted from ctx_builder output
    # (e.g. "trending_up", "choppy"). None if the ctx_builder doesn't
    # populate one — preserves backward compatibility with old strategies.
    regime: str | None = None


def _atr(hist: list[BarData], period: int = 14) -> float:
    if not hist:
        return 0.0
    s = hist[-period:]
    return sum(b.high - b.low for b in s) / len(s)


class BacktestEngine:
    """Runs a FeaturePipeline + confluence rules across a bar stream."""

    def __init__(
        self,
        pipeline: FeaturePipeline,
        config: BacktestConfig,
        ctx_builder: Any | None = None,  # noqa: ANN401 - intentionally Any: ctx-builder protocol is duck-typed
        strategy_id: str = "eta_default",
        scorer: Callable[..., ConfluenceResult] | None = None,
        block_regimes: frozenset[str] | set[str] | None = None,
        require_ctx_true: tuple[str, ...] | None = None,
        strategy: Any | None = None,  # noqa: ANN401 - duck-typed Strategy protocol
        on_trade_close: Callable[[Trade], None] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.config = config
        self.ctx_builder = ctx_builder or (lambda bar, hist: {})
        self.strategy_id = strategy_id
        # Confluence scorer. Defaults to the global 5-feature scorer.
        # Pass score_confluence_mnq (or any other 5-tuple-accepting
        # callable) to swap weights without subclassing.
        self.scorer = scorer or score_confluence
        # Optional regime gate: any ctx["regime"] in this set causes
        # _enter() to refuse new positions. Built for the 2026-04-27
        # MNQ Window 0 finding (strategy bleeds in trending regimes,
        # +EV in choppy). Default None preserves legacy no-gate
        # behaviour for every existing caller.
        self.block_regimes = (
            frozenset(block_regimes) if block_regimes is not None else None
        )
        # ctx-flag gate: every key listed here must be truthy in the
        # ctx dict for _enter() to proceed. Built for the 2026-04-27
        # MNQ optimization stack (session_ok blocks first/last 30m
        # of RTH; es_aligned blocks decoupled-from-ES sessions).
        # Empty tuple / None means "no flag gate" — legacy callers
        # are unaffected.
        self.require_ctx_true: tuple[str, ...] = tuple(require_ctx_true or ())
        # Pluggable strategy. When set, _enter() delegates entirely to
        # strategy.maybe_enter() and the confluence-scoring path is
        # bypassed. ORBStrategy is the canonical implementation —
        # see strategies/orb_strategy.py. Any object exposing
        # ``maybe_enter(bar, hist, equity, config) -> _Open | None``
        # works (Protocol-style; no ABC required).
        self.strategy = strategy
        # Trade-close callback. Fires once per realized trade with
        # the full Trade object (pnl_r, pnl_usd, side, exit_reason,
        # etc.). Built for AdaptiveKellySizing's trade-level ledger
        # — proper trade-PnL signal vs the previous equity-delta
        # inference. Optional; None = no callback (legacy behaviour).
        # The callback runs in-engine BEFORE the trade is appended
        # to the trades list and the equity is updated, but AFTER
        # _close() has produced the realized Trade. If the callback
        # raises, the engine swallows the exception so a buggy
        # listener can't break the backtest. Caller is responsible
        # for keeping the callback fast (it runs on every closed
        # trade in walk-forward → many invocations).
        self._on_trade_close = on_trade_close
        # Audit: count callback invocations + exceptions for
        # post-mortem visibility when something goes wrong.
        self._n_callback_invocations: int = 0
        self._n_callback_exceptions: int = 0

    def run(self, bars: Iterable[BarData]) -> BacktestResult:
        equity, curve, trades, hist = self.config.initial_equity, [], [], []
        curve.append(equity)
        open_t: _Open | None = None
        last_day, n_today = None, 0
        for bar in bars:
            hist.append(bar)
            day = bar.timestamp.date()
            if day != last_day:
                n_today, last_day = 0, day
            if open_t is not None:
                closed = self._exit(open_t, bar)
                if closed is not None:
                    self._fire_close_callback(closed)
                    equity += closed.pnl_usd
                    curve.append(equity)
                    trades.append(closed)
                    open_t = None
            if open_t is None and n_today < self.config.max_trades_per_day:
                opened = self._enter(bar, hist, equity)
                if opened is not None:
                    open_t, n_today = opened, n_today + 1
            if open_t is not None:
                adv = (
                    (open_t.entry_price - bar.low) * open_t.qty
                    if open_t.side == "BUY"
                    else (bar.high - open_t.entry_price) * open_t.qty
                )
                if adv > open_t.peak_adverse:
                    open_t.peak_adverse = max(0.0, adv)
        if open_t is not None and hist:
            final = self._close(open_t, hist[-1], hist[-1].close)
            self._fire_close_callback(final)
            equity += final.pnl_usd
            curve.append(equity)
            trades.append(final)
        return self._finalize(trades, curve)

    # ── Trade-close callback infrastructure ──
    def attach_trade_close_callback(
        self, callback: Callable[[Trade], None] | None,
    ) -> None:
        """Attach (or detach) a trade-close callback after construction.

        Useful when the strategy wraps the engine and needs to register
        its own listener at startup. ``None`` detaches.
        """
        self._on_trade_close = callback

    def _fire_close_callback(self, trade: Trade) -> None:
        """Invoke the trade-close callback with exception isolation."""
        if self._on_trade_close is None:
            return
        self._n_callback_invocations += 1
        try:
            self._on_trade_close(trade)
        except Exception:  # noqa: BLE001 - listener isolation
            self._n_callback_exceptions += 1

    @property
    def callback_stats(self) -> dict[str, int]:
        return {
            "invocations": self._n_callback_invocations,
            "exceptions": self._n_callback_exceptions,
        }

    # ── Entry / exit ──

    def _enter(self, bar: BarData, hist: list[BarData], equity: float) -> _Open | None:
        # Pluggable strategy short-circuits the confluence path entirely.
        # The ORB strategy doesn't need ctx_builder, scorer, or regime
        # gates — it has its own session/range/EMA filters.
        if self.strategy is not None:
            return self.strategy.maybe_enter(bar, hist, equity, self.config)
        ctx = self.ctx_builder(bar, hist)
        # Regime gate runs before scoring so a blocked regime never
        # consumes the trades-per-day budget. Both the gate set and
        # the regime tag are optional — when either is None we skip
        # silently and the legacy code path runs unchanged.
        if self.block_regimes is not None:
            current_regime = ctx.get("regime")
            if current_regime is not None and str(current_regime) in self.block_regimes:
                return None
        # ctx-flag gate. Each named key must be truthy. Cheap check
        # before scoring; missing keys evaluate as falsy and block
        # entry — that's the conservative default (don't trade when
        # the flag is unset because the data was unavailable).
        for key in self.require_ctx_true:
            if not ctx.get(key, False):
                return None
        results = self.pipeline.compute_all(bar, ctx)
        score = self.scorer(*self.pipeline.to_confluence_inputs(results))
        if score.total_score < self.config.confluence_threshold or score.recommended_leverage <= 0:
            return None
        atr = _atr(hist)
        if atr <= 0.0:
            return None
        side = "BUY" if float(ctx.get("bias", 1)) >= 0 else "SELL"
        risk_usd = equity * self.config.risk_per_trade_pct
        stop_dist = self.config.atr_stop_mult * atr
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None
        # NOTE: target_r_multiple / stop_r_multiple is the RR ratio
        # (default 3/2 = 1.5), so winning trades cap at +1.5R from
        # entry — that's why the demo report shows no trades in the
        # >2R bucket. To allow >2R winners, raise target_r_multiple
        # (or refactor target_r_multiple to mean R-distance directly).
        rr = self.config.target_r_multiple / self.config.stop_r_multiple
        stop = bar.close - stop_dist if side == "BUY" else bar.close + stop_dist
        target = bar.close + rr * stop_dist if side == "BUY" else bar.close - rr * stop_dist
        # Regime is conventionally surfaced by the ctx_builder under
        # the "regime" key. Falls back to None for legacy ctx builders.
        regime_raw = ctx.get("regime")
        regime = str(regime_raw) if regime_raw is not None else None
        return _Open(
            entry_bar=bar,
            side=side,
            qty=qty,
            entry_price=bar.close,
            stop=stop,
            target=target,
            risk_usd=risk_usd,
            confluence=score.total_score,
            leverage=float(score.recommended_leverage),
            regime=regime,
        )

    def _exit(self, t: _Open, bar: BarData) -> Trade | None:
        stop_hit = (t.side == "BUY" and bar.low <= t.stop) or (t.side == "SELL" and bar.high >= t.stop)
        tgt_hit = (t.side == "BUY" and bar.high >= t.target) or (t.side == "SELL" and bar.low <= t.target)
        if stop_hit:
            return self._close(t, bar, t.stop, exit_reason="stop_hit")
        if tgt_hit:
            return self._close(t, bar, t.target, exit_reason="target_hit")
        return None

    def _close(
        self,
        t: _Open,
        bar: BarData,
        exit_price: float,
        *,
        exit_reason: str = "session_end",
    ) -> Trade:
        direction = 1.0 if t.side == "BUY" else -1.0
        pnl_usd = direction * (exit_price - t.entry_price) * t.qty
        pnl_r = pnl_usd / t.risk_usd if t.risk_usd > 0.0 else 0.0
        return Trade(
            entry_time=t.entry_bar.timestamp,
            exit_time=bar.timestamp,
            symbol=t.entry_bar.symbol,
            side=t.side,  # type: ignore[arg-type]
            qty=t.qty,
            entry_price=t.entry_price,
            exit_price=exit_price,
            pnl_r=round(pnl_r, 4),
            pnl_usd=round(pnl_usd, 2),
            confluence_score=round(t.confluence, 2),
            leverage_used=t.leverage,
            max_drawdown_during=round(t.peak_adverse, 2),
            regime=t.regime,
            exit_reason=exit_reason,
        )

    def _finalize(self, trades: list[Trade], curve: list[float]) -> BacktestResult:
        n = len(trades)
        wins = [t for t in trades if t.pnl_r > 0.0]
        losses = [t for t in trades if t.pnl_r <= 0.0]
        start, end = curve[0], curve[-1]
        rets = [(c - p) / p for p, c in zip(curve, curve[1:], strict=False) if p > 0.0]
        return BacktestResult(
            strategy_id=self.strategy_id,
            n_trades=n,
            win_rate=round(len(wins) / n, 4) if n else 0.0,
            avg_win_r=round(sum(t.pnl_r for t in wins) / len(wins), 4) if wins else 0.0,
            avg_loss_r=round(abs(sum(t.pnl_r for t in losses) / len(losses)), 4) if losses else 0.0,
            expectancy_r=compute_expectancy(trades),
            profit_factor=compute_profit_factor(trades),
            sharpe=compute_sharpe(rets),
            sortino=compute_sortino(rets),
            max_dd_pct=compute_max_dd(curve),
            total_return_pct=round((end - start) / start * 100.0 if start > 0 else 0.0, 4),
            trades=trades,
        )
