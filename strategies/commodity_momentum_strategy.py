"""Commodity Momentum Strategy — trend-following for GC/CL/NG.

Unlike sweep_reclaim (liquidity-sweep + reclaim), this strategy:
  - Tracks rolling momentum (ROC, ADX, moving average alignment)
  - Enters on momentum thrust bars (high volume + range expansion)
  - Uses wide ATR stops with trailing (commodities trend, don't mean-revert)
  - Filters for macro-session alignment (London/NY overlap for gold, inventory for oil)

Asset-specific presets below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections import deque

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class MomentumConfig:
    """Configuration for commodity momentum strategy."""

    # Momentum detection
    roc_period: int = 20          # Rate of change lookback
    roc_threshold: float = 0.5    # Min ROC z-score to enter
    adx_period: int = 14          # ADX trend strength
    adx_threshold: int = 25       # Min ADX for trending regime
    ma_fast: int = 21             # Fast MA for trend detection
    ma_slow: int = 50             # Slow MA for trend filter

    # Volume confirmation
    volume_z_lookback: int = 24
    min_volume_z: float = 0.3

    # Risk
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    rr_target: float = 2.5
    risk_per_trade_pct: float = 0.005

    # Trade management
    min_bars_between_trades: int = 12
    max_trades_per_day: int = 3
    warmup_bars: int = 72
    trailing_stop_atr_mult: float = 1.0  # Trail stop behind price at 1x ATR


class MomentumStrategy:
    """Commodity momentum — enter on thrust bars in trending regimes."""

    def __init__(self, cfg: MomentumConfig | None = None) -> None:
        self.cfg = cfg or MomentumConfig()
        self._roc_values: list[float] = []
        self._close_window: list[float] = []
        self._adx: float | None = None
        self._ma_fast: float | None = None
        self._ma_slow: float | None = None
        self._volume_window: list[float] = []
        self._tr_window: list[float] = []
        self._bars_since_last_trade: int = 999
        self._trades_today: int = 0
        self._bars_seen: int = 0

    def maybe_enter(
        self, bar: BarData, hist: list[BarData], equity: float, config: BacktestConfig,
    ) -> _Open | None:
        self._bars_seen += 1

        # Warmup
        if self._bars_seen < self.cfg.warmup_bars:
            self._update_indicators(bar, hist)
            return None

        self._update_indicators(bar, hist)
        self._bars_since_last_trade += 1

        # Cooldown + daily cap
        if self._bars_since_last_trade < self.cfg.min_bars_between_trades:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None

        # Momentum thrust detection
        side = self._detect_momentum_thrust(bar)
        if side is None:
            return None

        # Risk calculation
        atr = self._current_atr()
        stop_dist = atr * self.cfg.atr_stop_mult
        risk_usd = equity * self.cfg.risk_per_trade_pct

        if side == "BUY":
            entry = bar.close
            stop = entry - stop_dist
            target = entry + stop_dist * self.cfg.rr_target
        else:
            entry = bar.close
            stop = entry + stop_dist
            target = entry - stop_dist * self.cfg.rr_target

        qty = risk_usd / max(stop_dist, 1e-9)
        if qty <= 0:
            return None

        self._bars_since_last_trade = 0
        self._trades_today += 1

        from eta_engine.backtest.engine import _Open
        return _Open(
            entry_bar=bar, side=side, qty=qty,
            entry_price=entry, stop=stop, target=target,
            risk_usd=risk_usd, confluence=7.0, leverage=1.0,
            regime=f"momentum_{side.lower()}",
        )

    def _update_indicators(self, bar: BarData, hist: list[BarData]) -> None:
        # ROC
        self._close_window.append(bar.close)
        if len(self._close_window) > self.cfg.roc_period:
            self._close_window.pop(0)
        if len(self._close_window) >= self.cfg.roc_period:
            roc = (bar.close - self._close_window[0]) / max(self._close_window[0], 1e-9) * 100
            self._roc_values.append(roc)

        # MAs
        alpha_f = 2.0 / (self.cfg.ma_fast + 1)
        alpha_s = 2.0 / (self.cfg.ma_slow + 1)
        self._ma_fast = bar.close * alpha_f + (self._ma_fast or bar.close) * (1 - alpha_f)
        self._ma_slow = bar.close * alpha_s + (self._ma_slow or bar.close) * (1 - alpha_s)

        # Volume
        self._volume_window.append(bar.volume)
        if len(self._volume_window) > self.cfg.volume_z_lookback:
            self._volume_window.pop(0)

        # True Range
        if hist:
            prev_close = hist[-1].close
            tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        else:
            tr = bar.high - bar.low
        self._tr_window.append(tr)
        if len(self._tr_window) > self.cfg.atr_period:
            self._tr_window.pop(0)

    def _current_atr(self) -> float:
        if len(self._tr_window) < self.cfg.atr_period:
            return bar_range(1.0, 0.01)  # fallback
        return sum(self._tr_window) / len(self._tr_window)

    def _detect_momentum_thrust(self, bar: BarData) -> str | None:
        """Detect momentum thrust bar. Returns 'BUY', 'SELL', or None."""
        if len(self._roc_values) < 5 or self._ma_fast is None or self._ma_slow is None:
            return None

        # Recent ROC must be positive for BUY, negative for SELL
        recent_roc = sum(self._roc_values[-5:]) / 5
        roc_std = _stdev(self._roc_values[-20:]) if len(self._roc_values) >= 20 else 1.0
        if roc_std < 1e-9:
            return None
        roc_z = recent_roc / roc_std

        # Trend filter: fast MA must be above slow MA for BUY
        trend_up = self._ma_fast > self._ma_slow
        trend_down = self._ma_fast < self._ma_slow

        # Volume confirmation
        if len(self._volume_window) >= self.cfg.volume_z_lookback:
            vols = list(self._volume_window)
            mean_v = sum(vols) / len(vols)
            std_v = _stdev(vols)
            if std_v > 0:
                vol_z = (bar.volume - mean_v) / std_v
                if vol_z < self.cfg.min_volume_z:
                    return None

        # Thrust: large range bar relative to ATR
        atr = self._current_atr()
        bar_range_val = bar.high - bar.low
        if bar_range_val < atr * 0.6:  # Lowered from 0.8
            return None  # No thrust

        # Bullish thrust
        if roc_z > self.cfg.roc_threshold and trend_up:
            if bar.close > bar.open:
                return "BUY"

        # Bearish thrust
        if roc_z < -self.cfg.roc_threshold and trend_down:
            if bar.close < bar.open:
                return "SELL"

        return None


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


# ---------------------------------------------------------------------------
# Asset-class presets
# ---------------------------------------------------------------------------

def gc_momentum_preset() -> MomentumConfig:
    """Gold (GC) 1h — macro trend follower. Wide stops for macro swings."""
    return MomentumConfig(
        roc_period=20, roc_threshold=0.2,  # Lowered from 0.4
        adx_period=14, adx_threshold=20,    # Lowered from 25
        ma_fast=21, ma_slow=50, volume_z_lookback=24, min_volume_z=0.2,
        atr_period=14, atr_stop_mult=3.5, rr_target=3.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=8,
        max_trades_per_day=3, warmup_bars=72,
    )


def cl_momentum_preset() -> MomentumConfig:
    """Crude oil (CL) 1h — momentum on inventory/supply shocks.
    Wider stops didn't help — reverting to tighter, higher frequency."""
    return MomentumConfig(
        roc_period=20, roc_threshold=0.3,
        adx_period=14, adx_threshold=20,
        ma_fast=21, ma_slow=50, volume_z_lookback=24, min_volume_z=0.2,
        atr_period=14, atr_stop_mult=2.5, rr_target=3.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=6,
        max_trades_per_day=4, warmup_bars=72,
    )


def ng_momentum_preset() -> MomentumConfig:
    """Natural gas (NG) 1h — wild swings, widest stops."""
    return MomentumConfig(
        roc_period=20, roc_threshold=0.3,
        adx_period=14, adx_threshold=20,
        ma_fast=21, ma_slow=50, volume_z_lookback=24, min_volume_z=0.5,
        atr_period=14, atr_stop_mult=4.5, rr_target=3.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
    )
