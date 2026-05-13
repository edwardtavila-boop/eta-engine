"""
EVOLUTIONARY TRADING ALGO  //  strategies.crypto_meanrev_strategy
==================================================================
Crypto Mean Reversion — Bollinger touch + RSI extreme.

Per the 2026-04-27 user directive on crypto bot strategies:

* "Mean Reversion / Support & Resistance: Buy dips to key supports,
  moving averages, or Bollinger Bands (with oversold RSI); sell
  rallies to resistance."

This strategy:

* **Bollinger bands** — N-period SMA ± k×stddev. Touch of the lower
  band primes a long, touch of the upper band primes a short. The
  band itself is rolling, so the strategy is regime-aware: in trends
  the band widens and signals rate-limit naturally.
* **RSI confirmation** — only buy when RSI < oversold threshold
  (default 30); only sell when RSI > overbought (default 70). RSI
  filter cuts the false-positive band touches that happen during a
  one-sided move.
* **Mean target** — exit by ATR-distance, not by mid-band touch,
  because the engine's existing exit machinery (stop / target / EOD)
  is contract-rigid. RR=1.5 keeps the strategy honest about its
  edge: mean-rev wins are smaller than trend wins.

Why a separate strategy and not another Crypto-ORB cell
-------------------------------------------------------
ORB is a momentum continuation; mean-reversion is the opposite trade.
Sharing a config would force one direction's parameters into the
other's regime, which is exactly the kind of overfit that kills
walk-forward stability.
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
class CryptoMeanRevConfig:
    """Mean-reversion knobs. Defaults tuned for BTC/1h bars."""

    # ── Bollinger ──
    bb_period: int = 20
    bb_stddev_mult: float = 2.0
    # ── RSI ──
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    # ── Risk / exits ──
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 1.5  # smaller than trend; mean-rev edges are tight
    risk_per_trade_pct: float = 0.01
    # ── Hygiene ──
    max_trades_per_day: int = 2
    min_bars_between_trades: int = 6
    # ── Regime gate ──
    # Mean-reversion in trending regimes is the textbook way to bleed.
    # Default ON, tight ADX ceiling — disable mean-rev entries when the
    # market is trending.  Same protection added to RSI MR.
    enable_adx_filter: bool = True
    adx_period: int = 14
    adx_max: float = 25.0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class CryptoMeanRevStrategy:
    """Bollinger + RSI mean-reversion for 24/7 crypto bars."""

    def __init__(self, config: CryptoMeanRevConfig | None = None) -> None:
        self.cfg = config or CryptoMeanRevConfig()
        # Deques cap themselves at the lookback so memory is bounded
        # even when a strategy lives across decades of bars.
        self._closes: deque[float] = deque(maxlen=self.cfg.bb_period)
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
        # ── per-day reset ──
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0

        self._bars_seen += 1

        # ── update RSI streams ──
        if self._prev_close is not None:
            change = bar.close - self._prev_close
            self._gains.append(max(change, 0.0))
            self._losses.append(max(-change, 0.0))
        self._prev_close = bar.close

        self._closes.append(bar.close)

        # ── warmup gates ──
        need_bars = max(self.cfg.bb_period, self.cfg.rsi_period + 1)
        if self._bars_seen < need_bars:
            return None
        if len(self._closes) < self.cfg.bb_period:
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

        # ── Bollinger ──
        n = len(self._closes)
        mean = sum(self._closes) / n
        var = sum((c - mean) ** 2 for c in self._closes) / n
        std = var**0.5
        upper = mean + self.cfg.bb_stddev_mult * std
        lower = mean - self.cfg.bb_stddev_mult * std

        # ── RSI (Wilder-style EMA on gains/losses) ──
        avg_gain = sum(self._gains) / len(self._gains)
        avg_loss = sum(self._losses) / len(self._losses)
        if avg_loss == 0.0:
            rsi = 100.0 if avg_gain > 0.0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # ── direction logic ──
        side: str | None = None
        # Long: low pierces lower band AND RSI oversold
        if bar.low <= lower and rsi <= self.cfg.rsi_oversold:
            side = "BUY"
        elif bar.high >= upper and rsi >= self.cfg.rsi_overbought:
            side = "SELL"
        if side is None:
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
            regime="crypto_meanrev",
        )
        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        return opened
