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
from eta_engine.core.confluence_scorer import score_confluence

if TYPE_CHECKING:
    from collections.abc import Iterable

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
        ctx_builder: Any | None = None,
        strategy_id: str = "apex_default",
    ) -> None:
        self.pipeline = pipeline
        self.config = config
        self.ctx_builder = ctx_builder or (lambda bar, hist: {})
        self.strategy_id = strategy_id

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
            equity += final.pnl_usd
            curve.append(equity)
            trades.append(final)
        return self._finalize(trades, curve)

    # ── Entry / exit ──

    def _enter(self, bar: BarData, hist: list[BarData], equity: float) -> _Open | None:
        ctx = self.ctx_builder(bar, hist)
        results = self.pipeline.compute_all(bar, ctx)
        score = score_confluence(*self.pipeline.to_confluence_inputs(results))
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
        rr = self.config.target_r_multiple / self.config.stop_r_multiple
        stop = bar.close - stop_dist if side == "BUY" else bar.close + stop_dist
        target = bar.close + rr * stop_dist if side == "BUY" else bar.close - rr * stop_dist
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
        )

    def _exit(self, t: _Open, bar: BarData) -> Trade | None:
        stop_hit = (t.side == "BUY" and bar.low <= t.stop) or (t.side == "SELL" and bar.high >= t.stop)
        tgt_hit = (t.side == "BUY" and bar.high >= t.target) or (t.side == "SELL" and bar.low <= t.target)
        if stop_hit:
            return self._close(t, bar, t.stop)
        if tgt_hit:
            return self._close(t, bar, t.target)
        return None

    def _close(self, t: _Open, bar: BarData, exit_price: float) -> Trade:
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
