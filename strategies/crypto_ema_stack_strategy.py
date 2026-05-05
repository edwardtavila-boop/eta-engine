"""
EVOLUTIONARY TRADING ALGO  //  strategies.crypto_ema_stack_strategy
====================================================================
EMA-stack trend continuation — the supercharged sibling of
crypto_regime_trend.

User insight (2026-04-27, follow-up): "what about when the 21 EMA is
above the [N] for scalping/swing, 9 over 21 etc — what variants would
increase this to where we need it?"

Where regime_trend gates on a single slow EMA (default 200), this
strategy gates on a *stack* of EMAs all aligned in the trade
direction. That's the classic ribbon rule: longs only when
9 > 21 > 50 > 200 (or whatever subset you configure).

Six independently-configurable variants
---------------------------------------

A. **Stack alignment** (`stack_periods`): list of EMA periods that
   must be ordered correctly (descending close→fast for longs,
   ascending for shorts). Common configs:
     * [9, 21]            — pure scalp, fast trend
     * [9, 21, 50]        — swing
     * [9, 21, 50, 200]   — full stack, position trade

B. **Entry EMA** (`entry_ema_period`): which EMA is the pullback
   target. Pullback to 9 = sharpest dips, fewer trades. Pullback
   to 50 = longer dips, more conservative entries.

C. **Stack separation filter** (`min_stack_spread_atr`): require
   the spread between the fastest and slowest stack EMAs to be at
   least N x ATR. Skips chop / compression zones where EMAs hug.

D. **Volume confirmation** (`volume_mult` + `volume_lookback`):
   pullback bar must clock > volume_mult x recent_avg volume.
   Distinguishes institutional dips from grindy chop.

E. **Adaptive RR** (`adaptive_rr_enabled` + `tightness_rr_lift`):
   when the stack is compressed (spread < N x ATR), expansion
   moves are statistically larger — bump RR by a multiplier.

F. **Soft stop on entry-EMA reclaim** (`soft_stop_enabled`): exit
   immediately if close prints back through the entry EMA against
   the trade direction. Limits drawdown without waiting for the
   full ATR stop. Disabled by default to keep the engine's exit
   contract clean — when enabled, the strategy emits a tight stop
   at the entry EMA distance instead of the ATR distance.

The walk-forward sweep tests each variant in isolation + combined
to find the cell that maximizes OOS Sharpe over the +2.96 baseline.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


def _default_stack() -> tuple[int, ...]:
    return (9, 21, 50, 200)


@dataclass(frozen=True)
class CryptoEmaStackConfig:
    """All six variants tunable in one config.

    Defaults are the "full stack swing" mode: 9>21>50>200 alignment,
    pullback to 21, light stack-separation gate, no volume confirm,
    no adaptive RR, no soft stop. Sweep over these to find the
    maximum-edge cell.
    """

    # A. Stack alignment
    stack_periods: tuple[int, ...] = field(default_factory=_default_stack)

    # B. Entry EMA — index into stack_periods
    entry_ema_idx: int = 1  # 0=9, 1=21, 2=50, 3=200
    entry_tolerance_pct: float = 0.5  # bar.low within X% of entry EMA

    # C. Stack separation filter
    min_stack_spread_atr: float = 0.0  # 0 = disabled

    # D. Volume confirmation
    volume_mult: float = 0.0  # 0 = disabled; >0 requires X * avg
    volume_lookback: int = 20

    # E. Adaptive RR
    adaptive_rr_enabled: bool = False
    tightness_threshold_atr: float = 1.5  # spread < this → "tight"
    tightness_rr_lift: float = 1.5  # multiply RR by this when tight

    # F. Soft stop on entry-EMA reclaim
    soft_stop_enabled: bool = False

    # Risk / exits (base)
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 2.5
    risk_per_trade_pct: float = 0.01

    # Hygiene
    min_bars_between_trades: int = 12
    max_trades_per_day: int = 3
    warmup_bars: int = 220


def _ema_step(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return value
    alpha = 2.0 / (period + 1)
    return alpha * value + (1 - alpha) * prev


class CryptoEmaStackStrategy:
    """EMA-stack alignment + pullback continuation."""

    def __init__(self, config: CryptoEmaStackConfig | None = None) -> None:
        self.cfg = config or CryptoEmaStackConfig()
        if not self.cfg.stack_periods:
            raise ValueError("stack_periods must be non-empty")
        if not (0 <= self.cfg.entry_ema_idx < len(self.cfg.stack_periods)):
            raise ValueError(
                f"entry_ema_idx {self.cfg.entry_ema_idx} out of range for "
                f"stack of length {len(self.cfg.stack_periods)}"
            )
        # Stack must be sorted ascending by period (9, 21, 50, 200) so
        # bull stack = ema[0] > ema[1] > ... > ema[N-1] (fast > slow).
        if list(self.cfg.stack_periods) != sorted(self.cfg.stack_periods):
            raise ValueError(
                "stack_periods must be ascending by period (e.g. (9, 21, 50, 200))"
            )

        self._emas: list[float | None] = [None] * len(self.cfg.stack_periods)
        self._volumes: deque[float] = deque(maxlen=self.cfg.volume_lookback)
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None

    # -- helpers --------------------------------------------------------------

    def _stack_aligned(self, side: str, emas: list[float | None] | None = None) -> bool:
        """True iff every EMA pair in the stack is ordered correctly."""
        if emas is None:
            emas = self._emas
        if any(e is None for e in emas):
            return False
        if side == "BUY":
            # Bull: fast > slow at every adjacent pair
            return all(emas[i] > emas[i + 1] for i in range(len(emas) - 1))
        # Bear: fast < slow at every adjacent pair
        return all(emas[i] < emas[i + 1] for i in range(len(emas) - 1))

    def _stack_spread_atr_ratio(self, atr: float, emas: list[float | None] | None = None) -> float:
        """Spread between fastest and slowest EMA divided by ATR.

        Used by the stack-separation filter and the adaptive RR.
        Returns 0.0 if any EMA is None or ATR is zero.
        """
        if emas is None:
            emas = self._emas
        if any(e is None for e in emas) or atr <= 0.0:
            return 0.0
        spread = abs(emas[0] - emas[-1])
        return spread / atr

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0
        self._bars_seen += 1

        # snapshot prior bar's EMA — see same-bar self-reference fix
        prev_emas: list[float | None] = list(self._emas)
        # Update EMAs every bar
        for i, period in enumerate(self.cfg.stack_periods):
            self._emas[i] = _ema_step(self._emas[i], bar.close, period)
        self._volumes.append(bar.volume)

        # Warmup
        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if any(e is None for e in prev_emas):
            return None
        if len(hist) < self.cfg.atr_period + 1:
            return None

        # Per-trade latches
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx)
            < self.cfg.min_bars_between_trades
        ):
            return None

        # Determine direction from stack alignment (use prior-bar snapshot)
        side: str | None = None
        if self._stack_aligned("BUY", prev_emas):
            side = "BUY"
        elif self._stack_aligned("SELL", prev_emas):
            side = "SELL"
        if side is None:
            return None

        # ATR sizing (computed early so the spread filter can use it)
        atr_window = hist[-self.cfg.atr_period:] if hist else []
        if len(atr_window) < 2:
            return None
        atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
        if atr <= 0.0:
            return None

        # C. Stack separation filter
        if self.cfg.min_stack_spread_atr > 0.0:
            spread_ratio = self._stack_spread_atr_ratio(atr, prev_emas)
            if spread_ratio < self.cfg.min_stack_spread_atr:
                return None

        # B. Pullback entry — bar's wick taps the entry EMA (prior-bar
        # snapshot), close is back on the regime side.
        entry_ema = prev_emas[self.cfg.entry_ema_idx]
        tol = self.cfg.entry_tolerance_pct / 100.0
        band_lo = entry_ema * (1.0 - tol)
        band_hi = entry_ema * (1.0 + tol)
        if side == "BUY":
            touched = bar.low <= band_hi
            bounced = bar.close > entry_ema
            within_tol = bar.low >= band_lo
            if not (touched and bounced and within_tol):
                return None
        else:  # SELL
            touched = bar.high >= band_lo
            bounced = bar.close < entry_ema
            within_tol = bar.high <= band_hi
            if not (touched and bounced and within_tol):
                return None

        # D. Volume confirmation
        if self.cfg.volume_mult > 0.0 and len(self._volumes) >= 5:
            avg_vol = sum(self._volumes) / len(self._volumes)
            if avg_vol > 0.0 and bar.volume < self.cfg.volume_mult * avg_vol:
                return None

        # E. Adaptive RR — bump RR when stack is compressed
        rr = self.cfg.rr_target
        if self.cfg.adaptive_rr_enabled:
            spread_ratio = self._stack_spread_atr_ratio(atr, prev_emas)
            if spread_ratio < self.cfg.tightness_threshold_atr:
                rr *= self.cfg.tightness_rr_lift

        # F. Soft stop — use the entry EMA as the stop instead of ATR
        if self.cfg.soft_stop_enabled:
            soft_dist = abs(bar.close - entry_ema)
            stop_dist = max(soft_dist, 0.5 * atr)  # floor so qty doesn't blow up
        else:
            stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None

        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None

        entry_price = bar.close
        if side == "BUY":
            stop = entry_price - stop_dist
            target = entry_price + rr * stop_dist
        else:
            stop = entry_price + stop_dist
            target = entry_price - rr * stop_dist

        from eta_engine.backtest.engine import _Open

        regime_tag = (
            f"ema_stack_{'bull' if side == 'BUY' else 'bear'}"
            f"_{len(self.cfg.stack_periods)}"
        )
        opened = _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry_price,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0, leverage=1.0,
            regime=regime_tag,
        )
        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        return opened
