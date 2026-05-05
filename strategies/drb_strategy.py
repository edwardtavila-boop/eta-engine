"""
EVOLUTIONARY TRADING ALGO  //  strategies.drb_strategy
========================================================
Daily Range Breakout — the daily-bar analog of ORB.

Why
---
ORB anchors on the first N minutes after RTH open. On daily bars
that concept doesn't exist — there's one bar per day, not 78.
But a related signal does: the *prior-day high/low*. Trading the
break of yesterday's range is a well-documented strategy on liquid
futures (NQ, ES, CL) and gets us 27 years of NQ1 daily history.

Why DRB instead of just running ORB on D bars
---------------------------------------------
ORB on daily fires zero trades because there is no intraday range
to compute — every bar IS the day. DRB flips it: today's bar
checks against *yesterday's* high/low. Same conceptual edge
(momentum breakout from a known reference), different anchor.

The strategy is intentionally simple — same fields as ORBConfig
where the meanings carry over (atr_stop_mult, rr_target, EMA bias).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class DRBConfig:
    """Daily-range-breakout knobs."""

    # Reference window: trade today against the high/low of the prior
    # ``lookback_days`` bars (default 1 = pure prior-day breakout).
    lookback_days: int = 1
    # Risk / exits — same semantics as ORBConfig
    rr_target: float = 2.0
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    risk_per_trade_pct: float = 0.01
    max_trades_per_day: int = 1
    # EMA bias: long only above EMA(N), short only below. 0 = disabled.
    ema_bias_period: int = 200
    # Min range filter: skip days where the prior range was too narrow
    # (avoids false breakouts in a tight consolidation).
    min_range_pts: float = 0.0


@dataclass
class _DRBState:
    """Tracking state per processed bar."""

    last_processed_date: object | None = None
    breakout_taken_today: bool = False
    trades_today: int = 0


class DRBStrategy:
    """Daily Range Breakout — break of prior N-day high/low."""

    def __init__(self, config: DRBConfig | None = None) -> None:
        self.cfg = config or DRBConfig()
        self._state = _DRBState()
        self._ema: float | None = None
        self._ema_alpha = (
            2.0 / (self.cfg.ema_bias_period + 1)
            if self.cfg.ema_bias_period > 0 else 0.0
        )

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Return _Open or None. Same engine contract as ORB.

        The hist passed in is every bar seen so far in the backtest
        window — the tail tells us yesterday's range, the body is
        EMA / ATR food.
        """
        # ── Per-day reset ──
        bar_date = bar.timestamp.date()
        if self._state.last_processed_date != bar_date:
            self._state = _DRBState(last_processed_date=bar_date)

        # ── EMA bias update ──
        if self.cfg.ema_bias_period > 0:
            if self._ema is None:
                self._ema = bar.close
            else:
                self._ema = self._ema_alpha * bar.close + (1 - self._ema_alpha) * self._ema

        # ── Latches ──
        if self._state.breakout_taken_today:
            return None
        if self._state.trades_today >= self.cfg.max_trades_per_day:
            return None

        # ── Need lookback bars BEFORE today ──
        # The engine appends the current bar to hist before calling
        # _enter, so hist[-1] IS today. We compare today against the
        # PRIOR bars, not against itself — exclude the tail.
        prior = hist[:-1] if hist and hist[-1] is bar else hist
        if len(prior) < self.cfg.lookback_days:
            return None

        # ── EMA warmup gate ──
        # The EMA bias filter is only meaningful once it has converged.
        # On a 30-day daily backtest the EMA-200 never converges, so any
        # entry would fire against an uninitialized EMA = noise. Require
        # at least ema_bias_period prior bars before allowing entries.
        if self.cfg.ema_bias_period > 0 and len(prior) < self.cfg.ema_bias_period:
            return None
        ref_bars = prior[-self.cfg.lookback_days:]
        ref_high = max(b.high for b in ref_bars)
        ref_low = min(b.low for b in ref_bars)
        ref_range = ref_high - ref_low
        if self.cfg.min_range_pts > 0.0 and ref_range < self.cfg.min_range_pts:
            return None

        # ── ATR for stop sizing ──
        atr_window = hist[-self.cfg.atr_period:] if hist else []
        if len(atr_window) < 2:
            return None
        atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
        if atr <= 0.0:
            return None

        # ── Direction + EMA bias ──
        ema = self._ema if self.cfg.ema_bias_period > 0 else bar.close
        side: str | None = None
        if bar.high > ref_high and (ema is None or bar.close >= ema):
            side = "BUY"
        elif bar.low < ref_low and (ema is None or bar.close <= ema):
            side = "SELL"
        if side is None:
            return None

        # ── Build the open trade ──
        from eta_engine.backtest.engine import _Open

        risk_usd = equity * self.cfg.risk_per_trade_pct
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None
        entry_price = bar.close
        if side == "BUY":
            stop = entry_price - stop_dist
            target = entry_price + self.cfg.rr_target * stop_dist
        else:
            stop = entry_price + stop_dist
            target = entry_price - self.cfg.rr_target * stop_dist

        opened = _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry_price,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0, leverage=1.0,
            regime="drb_breakout",
        )
        self._state.breakout_taken_today = True
        self._state.trades_today += 1
        return opened
