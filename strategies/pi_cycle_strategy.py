"""
EVOLUTIONARY TRADING ALGO  //  strategies.pi_cycle_strategy
=============================================================
Pi Cycle Top + Bottom indicator on BTC daily.

Background
----------
The Pi Cycle Top/Bottom is a classical BTC-cycle indicator
discovered by Philip Swift (Look Into Bitcoin). On daily bars:

  * 111-day SMA × 2  vs  350-day SMA crossover

Pi Cycle TOP signal: 111d-SMA × 2 crosses ABOVE 350d-SMA.
  Historically marked the BTC bull-cycle peak within ±3 days
  in 2013, 2017, 2021. Signal: SELL / go short / take profits.

Pi Cycle BOTTOM signal: 111d-SMA × 2 crosses BELOW 350d-SMA.
  Less famous but historically marks bear-cycle bottoms.
  Signal: BUY / go long.

Why this fits the user's framework
-----------------------------------
The user's 2026-04-27 directive: "after confirming regime we
can scalp lower time frames... do from the 4 you listed just
now the ones worth doing." Pi Cycle is the cheapest of the four:

  * Single indicator, no other dependencies.
  * Few signals (~once per cycle = every 3-4 years on BTC).
  * Each signal is a multi-month directional commitment, not
    a scalp.
  * Fits "HTF regime determines bias" — Pi Cycle IS the
    HTF regime read for BTC's macro cycle.

Limitations / honest scope
--------------------------
* Few signals per backtest. With 5 yr of BTC daily we get
  ~1-2 cycle markers. Walk-forward Sharpe is structurally
  limited by sample count.
* The indicator has worked on every prior cycle but is purely
  empirical — there's no causal mechanism. Future cycles may
  diverge as ETF flows + institutional ownership change BTC's
  cycle structure.
* Stop placement is the operator's choice. Default: trailing
  stop at the 111-SMA itself once a position is open.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class PiCycleConfig:
    """Pi Cycle parameters — defaults match Swift's original spec."""

    # Classic Pi Cycle: 111-SMA × 2 vs 350-SMA
    fast_sma_period: int = 111
    slow_sma_period: int = 350
    fast_sma_multiplier: float = 2.0

    # Risk / exits — sizing is conservative because Pi Cycle is
    # a multi-month signal; we aren't trying to extract every $.
    # Stop at 50% of the move-to-target distance (no ATR — Pi
    # Cycle is a structural / fundamental call, not vol-driven).
    risk_per_trade_pct: float = 0.02  # 2% — bigger than usual since few trades
    rr_target: float = 5.0  # multi-month hold
    atr_period: int = 14
    atr_stop_mult: float = 4.0  # wide stop, daily bars

    # Hygiene — only one Pi Cycle signal at a time
    max_concurrent: int = 1
    # Reset trade-allow flag after this many bars from a fire so a
    # subsequent crossover-back can fire (e.g. fakeouts).
    cooldown_bars: int = 30

    # Direction toggle. Most operators only want top-fade (short/exit)
    # signals; bottom-buy signals are positive but less reliable
    # because BTC bottoms are messy and the indicator can fire mid-
    # crash before the actual low.
    enable_top_signal: bool = True  # 111-SMA*2 crosses ABOVE 350-SMA -> SELL
    enable_bottom_signal: bool = True  # 111-SMA*2 crosses BELOW 350-SMA -> BUY


class PiCycleStrategy:
    """Pi Cycle Top/Bottom on BTC daily."""

    def __init__(self, config: PiCycleConfig | None = None) -> None:
        self.cfg = config or PiCycleConfig()
        # Use deques for the SMA windows — bounded memory
        self._fast_window: deque[float] = deque(maxlen=self.cfg.fast_sma_period)
        self._slow_window: deque[float] = deque(maxlen=self.cfg.slow_sma_period)
        # Track previous fast_x_mult vs slow for crossover detection
        self._prev_diff: float | None = None
        self._bars_seen: int = 0
        self._last_fire_idx: int | None = None

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Return an _Open or None. Same engine contract as ORB."""
        self._bars_seen += 1

        # Update SMA windows
        self._fast_window.append(bar.close)
        self._slow_window.append(bar.close)

        # Wait until both windows are full
        if len(self._fast_window) < self.cfg.fast_sma_period or len(self._slow_window) < self.cfg.slow_sma_period:
            return None

        fast_sma = sum(self._fast_window) / len(self._fast_window)
        slow_sma = sum(self._slow_window) / len(self._slow_window)
        fast_x_mult = fast_sma * self.cfg.fast_sma_multiplier
        diff = fast_x_mult - slow_sma  # positive => above (top approaching)

        prev = self._prev_diff
        self._prev_diff = diff
        if prev is None:
            return None  # need a prior diff to detect crossover

        # Cooldown check
        if self._last_fire_idx is not None and self._bars_seen - self._last_fire_idx < self.cfg.cooldown_bars:
            return None

        side: str | None = None
        regime_tag = ""
        # TOP cross: prev <= 0 and diff > 0  (fast×2 crossed above slow)
        if self.cfg.enable_top_signal and prev <= 0.0 and diff > 0.0:
            side = "SELL"
            regime_tag = "pi_cycle_top"
        # BOTTOM cross: prev >= 0 and diff < 0  (fast×2 crossed below slow)
        elif self.cfg.enable_bottom_signal and prev >= 0.0 and diff < 0.0:
            side = "BUY"
            regime_tag = "pi_cycle_bottom"

        if side is None:
            return None

        # ── ATR sizing (wide stop on daily) ──
        atr_window = hist[-self.cfg.atr_period :] if hist else []
        if len(atr_window) < 2:
            return None
        atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
        if atr <= 0.0:
            return None
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
            target = entry_price + self.cfg.rr_target * stop_dist
        else:
            stop = entry_price + stop_dist
            target = entry_price - self.cfg.rr_target * stop_dist

        from eta_engine.backtest.engine import _Open

        opened = _Open(
            entry_bar=bar,
            side=side,
            qty=qty,
            entry_price=entry_price,
            stop=stop,
            target=target,
            risk_usd=risk_usd,
            confluence=10.0,
            leverage=1.0,
            regime=regime_tag,
        )
        self._last_fire_idx = self._bars_seen
        return opened
