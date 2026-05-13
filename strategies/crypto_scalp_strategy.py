"""
EVOLUTIONARY TRADING ALGO  //  strategies.crypto_scalp_strategy
================================================================
Crypto Scalping / Momentum Breakouts — micro-level breaks on
short-term bars (1m / 5m / 15m).

Per the 2026-04-27 user directive on crypto bot strategies:

* "Scalping / Momentum Breakouts: Enter on short-term breaks (1–15 min
  charts) of micro levels, EMA crossovers (e.g., 9/21), VWAP rejections,
  or RSI/momentum signals."

This strategy isolates the *micro-level break* leg — break of the
recent N-bar high/low with VWAP alignment + RSI momentum. EMA-crossover
already lives in CryptoTrendStrategy, so this one stays focused on the
breakout half of the user's spec rather than smashing both into a
single noisy module.

Logic
-----
* **Micro-level** — rolling N-bar high/low (default N=20 on 5m bars =
  100 minutes). Break of the N-bar high primes long; break of N-bar
  low primes short.
* **VWAP filter** — running VWAP over the same lookback. Long must
  enter ABOVE VWAP (price has buyer support); short must enter BELOW.
* **RSI momentum** — RSI(14). Long requires RSI > 50 (momentum side);
  short requires RSI < 50. Cuts mean-reversion-style band touches that
  wear the same shape as a breakout.
* **Tight risk** — small ATR stop (0.8×) + RR 1.5 to match the speed
  of the timeframe. Scalpers can't afford 2.5R targets on 5m bars; the
  market doesn't give that much room before mean-reverting.

Scope discipline
----------------
This is the breakout leg. The retest/pullback leg is in
CryptoTrendStrategy. The funding-rate-arb leg from the user's spec
isn't a directional strategy at all — it's a market-neutral hedge that
needs perp-vs-spot pairs and a separate funding-stream consumer; out
of the backtest engine's contract scope and tracked as a separate
roadmap item.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoScalpConfig:
    """Scalper knobs. Defaults tuned for BTC/5m or ETH/5m."""

    # ── Micro-level ──
    lookback_bars: int = 20  # 100 min on 5m
    # ── VWAP ──
    vwap_lookback: int = 20  # session-anchored not always available; rolling instead
    require_vwap_alignment: bool = True
    # ── RSI ──
    rsi_period: int = 14
    rsi_long_min: float = 50.0
    rsi_short_max: float = 50.0
    # ── Risk / exits ──
    atr_period: int = 14
    atr_stop_mult: float = 0.8  # tight; scalper
    rr_target: float = 1.5
    risk_per_trade_pct: float = 0.005  # half of standard — scalp = volume

    # ── Hygiene ──
    max_trades_per_day: int = 6
    min_bars_between_trades: int = 4
    # Block re-entries within the same N-bar window: breakouts that
    # immediately reverse should not get a second shot.
    cooldown_after_loss_bars: int = 0  # set >0 in production


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class CryptoScalpStrategy:
    """Micro-level breakout + VWAP + RSI momentum scalper."""

    def __init__(self, config: CryptoScalpConfig | None = None) -> None:
        self.cfg = config or CryptoScalpConfig()
        self._highs: deque[float] = deque(maxlen=self.cfg.lookback_bars)
        self._lows: deque[float] = deque(maxlen=self.cfg.lookback_bars)
        self._vwap_pv: deque[float] = deque(maxlen=self.cfg.vwap_lookback)
        self._vwap_v: deque[float] = deque(maxlen=self.cfg.vwap_lookback)
        self._gains: deque[float] = deque(maxlen=self.cfg.rsi_period)
        self._losses: deque[float] = deque(maxlen=self.cfg.rsi_period)
        self._prev_close: float | None = None
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Return an _Open or None. Same engine contract as ORB."""
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0

        self._bars_seen += 1

        # Capture current N-bar high/low BEFORE updating the buffers so
        # today's bar can break the prior window's extreme rather than
        # tying with itself.
        prior_high = max(self._highs) if self._highs else None
        prior_low = min(self._lows) if self._lows else None

        # Update streams
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        typical = (bar.high + bar.low + bar.close) / 3.0
        self._vwap_pv.append(typical * bar.volume)
        self._vwap_v.append(bar.volume)
        if self._prev_close is not None:
            change = bar.close - self._prev_close
            self._gains.append(max(change, 0.0))
            self._losses.append(max(-change, 0.0))
        self._prev_close = bar.close

        # ── warmup gates ──
        need = max(self.cfg.lookback_bars, self.cfg.rsi_period + 1, self.cfg.vwap_lookback)
        if self._bars_seen < need:
            return None
        if prior_high is None or prior_low is None:
            return None
        if len(self._gains) < self.cfg.rsi_period:
            return None

        # ── per-trade latches ──
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
        ):
            return None

        # ── direction from N-bar break ──
        side: str | None = None
        if bar.high > prior_high:
            side = "BUY"
        elif bar.low < prior_low:
            side = "SELL"
        if side is None:
            return None

        # ── VWAP filter ──
        if self.cfg.require_vwap_alignment:
            v_sum = sum(self._vwap_v)
            if v_sum > 0.0:
                vwap = sum(self._vwap_pv) / v_sum
                if side == "BUY" and bar.close < vwap:
                    return None
                if side == "SELL" and bar.close > vwap:
                    return None

        # ── RSI filter ──
        avg_gain = sum(self._gains) / len(self._gains)
        avg_loss = sum(self._losses) / len(self._losses)
        if avg_loss == 0.0:
            rsi = 100.0 if avg_gain > 0.0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
        if side == "BUY" and rsi < self.cfg.rsi_long_min:
            return None
        if side == "SELL" and rsi > self.cfg.rsi_short_max:
            return None

        # ── ATR sizing ──
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
            regime="crypto_scalp",
        )
        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        return opened
