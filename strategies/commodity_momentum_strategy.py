"""Commodity Momentum Strategy — trend-following for GC/CL/NG.

Unlike sweep_reclaim (liquidity-sweep + reclaim), this strategy:
  - Tracks rolling momentum (ROC, ADX, moving average alignment)
  - Enters on momentum thrust bars (high volume + range expansion)
  - Uses wide ATR stops with trailing (commodities trend, don't mean-revert)
  - Filters for macro-session alignment (London/NY overlap for gold, inventory for oil)

Asset-specific presets below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class MomentumConfig:
    """Configuration for commodity momentum strategy."""

    # Momentum detection
    roc_period: int = 20  # Rate of change lookback
    roc_threshold: float = 0.5  # Min ROC z-score to enter
    adx_period: int = 14  # ADX trend strength
    adx_threshold: int = 25  # Min ADX for trending regime
    ma_fast: int = 21  # Fast MA for trend detection
    ma_slow: int = 50  # Slow MA for trend filter

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
    # Trailing stop multiplier — 2026-05-12 wave-4: implementation
    # landed (was dead code).  After price moves rr_trail_trigger * R
    # in favor, the stop trails behind price at trailing_stop_atr_mult
    # x ATR.  The supervisor exit-loop reads `trailing_stop` from
    # bot.open_position to know where to exit.
    trailing_stop_atr_mult: float = 1.0
    rr_trail_trigger: float = 1.0  # Activate trailing after price moves 1R

    # Vol-adjusted sizing — 2026-05-12 wave-4.  Same semantics as
    # SweepReclaimConfig; disabled by default = legacy.
    vol_adjusted_sizing: bool = False
    vol_baseline_window: int = 96
    vol_high_threshold: float = 1.5
    vol_low_threshold: float = 0.7
    vol_high_size_mult: float = 0.5
    vol_low_size_mult: float = 1.0


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
        # ADX state (Wilder smoothing).  Implemented 2026-05-12 — the
        # rationale string promised "ROC+ADX+MA thrust" but the prior
        # version declared adx_threshold without ever computing ADX.
        # Dead code → real filter.
        self._plus_dm_window: list[float] = []
        self._minus_dm_window: list[float] = []
        self._dx_window: list[float] = []
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        # ATR history for vol-adjusted sizing (wave-4)
        self._atr_history: list[float] = []

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
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

        # Track ATR for vol-adjusted sizing baseline
        self._atr_history.append(atr)
        if len(self._atr_history) > self.cfg.vol_baseline_window:
            self._atr_history.pop(0)

        # Vol-adjusted sizing (wave-4) — size DOWN in high-vol regimes
        if self.cfg.vol_adjusted_sizing and len(self._atr_history) >= self.cfg.vol_baseline_window // 2:
            sorted_atrs = sorted(self._atr_history)
            median_atr = sorted_atrs[len(sorted_atrs) // 2]
            if median_atr > 0:
                ratio = atr / median_atr
                if ratio >= self.cfg.vol_high_threshold:
                    risk_usd *= self.cfg.vol_high_size_mult
                elif ratio <= self.cfg.vol_low_threshold:
                    risk_usd *= self.cfg.vol_low_size_mult

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

        # Carry trailing-stop config on the regime field so the
        # supervisor's exit loop can read it back when computing
        # the live trailing-stop level.  No new schema change needed.
        regime = (
            f"momentum_{side.lower()}_trail{self.cfg.trailing_stop_atr_mult:.1f}"
            f"x_after_{self.cfg.rr_trail_trigger:.1f}R"
            if self.cfg.trailing_stop_atr_mult > 0
            else f"momentum_{side.lower()}"
        )
        return _Open(
            entry_bar=bar,
            side=side,
            qty=qty,
            entry_price=entry,
            stop=stop,
            target=target,
            risk_usd=risk_usd,
            confluence=7.0,
            leverage=1.0,
            regime=regime,
        )

    def compute_trailing_stop(
        self,
        side: str,
        entry_price: float,
        initial_stop: float,
        current_price: float,
        atr: float,
    ) -> float | None:
        """Compute the trailing-stop level for an open position.

        Called by the supervisor's exit loop on each bar.  Returns
        the new stop level (operator should ratchet — never widen
        the stop) or None if trailing should not activate yet.

        Activation:  price must move at least rr_trail_trigger * R
                     in favor (where R = |entry - initial_stop|).
        Trail level: entry_side - trailing_stop_atr_mult * ATR
                     (one-sided: only ratchet in profit direction).

        2026-05-12 wave-4: implements the previously-dead-code
        trailing_stop_atr_mult parameter.  Standalone helper so the
        supervisor's exit loop can drive it without re-entering the
        strategy.  Pure function — easy to unit-test.
        """
        if self.cfg.trailing_stop_atr_mult <= 0 or atr <= 0:
            return None
        r_distance = abs(entry_price - initial_stop)
        if r_distance <= 0:
            return None
        if side.upper() in ("BUY", "LONG"):
            move_in_favor = current_price - entry_price
            if move_in_favor < self.cfg.rr_trail_trigger * r_distance:
                return None
            new_stop = current_price - self.cfg.trailing_stop_atr_mult * atr
            # Never widen — only ratchet up
            return max(new_stop, initial_stop)
        # SHORT
        move_in_favor = entry_price - current_price
        if move_in_favor < self.cfg.rr_trail_trigger * r_distance:
            return None
        new_stop = current_price + self.cfg.trailing_stop_atr_mult * atr
        # Never widen — only ratchet down
        return min(new_stop, initial_stop)

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

        # True Range + directional movement (DM) for ADX
        if hist:
            prev_close = hist[-1].close
            tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        else:
            tr = bar.high - bar.low
        self._tr_window.append(tr)
        if len(self._tr_window) > self.cfg.atr_period:
            self._tr_window.pop(0)

        # Wilder ADX (2026-05-12 implementation — was dead code prior).
        # +DM = max(high - prev_high, 0) when up-move > down-move else 0
        # -DM = max(prev_low - low, 0) when down-move > up-move else 0
        # DI+ = 100 * SMA(+DM) / SMA(TR); DI- = 100 * SMA(-DM) / SMA(TR)
        # DX = 100 * |DI+ - DI-| / (DI+ + DI-)
        # ADX = SMA(DX) over adx_period
        if self._prev_high is not None and self._prev_low is not None:
            up_move = bar.high - self._prev_high
            down_move = self._prev_low - bar.low
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
            self._plus_dm_window.append(plus_dm)
            self._minus_dm_window.append(minus_dm)
            if len(self._plus_dm_window) > self.cfg.adx_period:
                self._plus_dm_window.pop(0)
            if len(self._minus_dm_window) > self.cfg.adx_period:
                self._minus_dm_window.pop(0)
            # Compute DX whenever we have a full window of TRs (uses
            # the SAME SMA of TR as our ATR calc — Wilder's ATR proxy).
            if len(self._plus_dm_window) >= self.cfg.adx_period and len(self._tr_window) >= self.cfg.adx_period:
                sum_plus_dm = sum(self._plus_dm_window)
                sum_minus_dm = sum(self._minus_dm_window)
                sum_tr = sum(self._tr_window)
                if sum_tr > 0:
                    di_plus = 100.0 * sum_plus_dm / sum_tr
                    di_minus = 100.0 * sum_minus_dm / sum_tr
                    di_total = di_plus + di_minus
                    if di_total > 0:
                        dx = 100.0 * abs(di_plus - di_minus) / di_total
                        self._dx_window.append(dx)
                        if len(self._dx_window) > self.cfg.adx_period:
                            self._dx_window.pop(0)
                        if len(self._dx_window) >= self.cfg.adx_period:
                            self._adx = sum(self._dx_window) / len(self._dx_window)
        self._prev_high = bar.high
        self._prev_low = bar.low

    def _current_atr(self) -> float:
        if len(self._tr_window) < self.cfg.atr_period:
            return max(self._tr_window[-1], 0.01) if self._tr_window else 1.0
        return sum(self._tr_window) / len(self._tr_window)

    def _detect_momentum_thrust(self, bar: BarData) -> str | None:
        """Detect momentum thrust bar. Returns 'BUY', 'SELL', or None."""
        if len(self._roc_values) < 5 or self._ma_fast is None or self._ma_slow is None:
            return None

        # ADX trending-regime gate (2026-05-12 — was advertised in
        # rationale but never enforced before this commit).  In a
        # chop regime (ADX < threshold), momentum strategies pay
        # round-trip slip on every false breakout — exactly the
        # failure mode gc_momentum exhibited in mid-2026 gold.
        if self._adx is None:
            return None  # Need ADX warmup before any entry
        if self._adx < self.cfg.adx_threshold:
            return None  # Chop regime — momentum is wrong tool

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
        if roc_z > self.cfg.roc_threshold and trend_up and bar.close > bar.open:
            return "BUY"

        # Bearish thrust
        if roc_z < -self.cfg.roc_threshold and trend_down and bar.close < bar.open:
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
    """Gold (GC) 1h — macro trend follower. Wide stops for macro swings.

    Wave-8 sizing kaizen (2026-05-12): risk_per_trade_pct cut 0.005 -> 0.0025
    after the dual-basis watchdog showed gc_momentum was USD-CRITICAL but
    R-HEALTHY. Per-trade USD risk on GC ($100/point) at the default 0.5%
    risk + 3.5x ATR stop = ~$147/R, against a -$200 USD retirement floor —
    a single stopped-out trade breached the threshold. Cut in half:
      Pre:  $/R_avg=$147, threshold=-$200, n_stopouts_to_breach=1.4
      Post: $/R_avg=~$74, threshold=-$200, n_stopouts_to_breach=2.7
    The R-multiple edge (+0.24R cumulative on n=8 trades) is preserved —
    only the USD scale of each trade is adjusted to give the operator
    more room to evaluate strategy edge before the threshold trips."""
    return MomentumConfig(
        roc_period=20,
        roc_threshold=0.2,  # Lowered from 0.4
        adx_period=14,
        adx_threshold=20,  # Lowered from 25
        ma_fast=21,
        ma_slow=50,
        volume_z_lookback=24,
        min_volume_z=0.2,
        atr_period=14,
        atr_stop_mult=3.5,
        rr_target=3.0,
        risk_per_trade_pct=0.0025,  # wave-8: halved from 0.005 (sizing kaizen)
        min_bars_between_trades=8,
        max_trades_per_day=3,
        warmup_bars=72,
    )


def cl_momentum_preset() -> MomentumConfig:
    """Crude oil (CL) 1h — momentum on inventory/supply shocks.
    Wider stops didn't help — reverting to tighter, higher frequency.

    Wave-8 sizing kaizen (2026-05-12): risk_per_trade_pct cut 0.005 -> 0.0025
    after the dual-basis watchdog showed cl_momentum was USD-CRITICAL
    (-$4,645) but R-HEALTHY (-1.71R cumulative on n=4). Per-trade USD risk
    on CL ($1,000/point) at 0.5% risk + 2.5x ATR stop = ~$1,654/R, against
    a -$1,500 USD retirement floor — a SINGLE stopped-out trade breached
    the threshold. Cut in half so the risk envelope matches the floor:
      Pre:  $/R_avg=$1,654, threshold=-$1,500, n_stopouts_to_breach=0.9
      Post: $/R_avg=~$827, threshold=-$1,500, n_stopouts_to_breach=1.8
    The R-multiple edge (-1.71R cumulative on small n=4) is unchanged;
    this addresses the SIZING failure surfaced by wave-7 dual-basis
    classification, not the strategy's R-edge.

    NB: cl_momentum's R-cumulative is currently negative (-1.71R) but n=4
    is too small to call the strategy itself broken. The 3-layer diamond
    protection holds; the wave-8 sizing fix gives the strategy more room
    to accumulate trades before the watchdog (or operator) makes a call."""
    return MomentumConfig(
        roc_period=20,
        roc_threshold=0.3,
        adx_period=14,
        adx_threshold=20,
        ma_fast=21,
        ma_slow=50,
        volume_z_lookback=24,
        min_volume_z=0.2,
        atr_period=14,
        atr_stop_mult=2.5,
        rr_target=3.0,
        risk_per_trade_pct=0.0025,  # wave-8: halved from 0.005 (sizing kaizen)
        min_bars_between_trades=6,
        max_trades_per_day=4,
        warmup_bars=72,
    )


def ng_momentum_preset() -> MomentumConfig:
    """Natural gas (NG) 1h — wild swings, widest stops."""
    return MomentumConfig(
        roc_period=20,
        roc_threshold=0.3,
        adx_period=14,
        adx_threshold=20,
        ma_fast=21,
        ma_slow=50,
        volume_z_lookback=24,
        min_volume_z=0.5,
        atr_period=14,
        atr_stop_mult=4.5,
        rr_target=3.5,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=72,
    )
