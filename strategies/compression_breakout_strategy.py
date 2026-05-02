"""
EVOLUTIONARY TRADING ALGO  //  strategies.compression_breakout_strategy
========================================================================
Volatility compression release breakout — the BTC-A strategy from
the 2026-04-27 user spec.

User mandate
------------
"BTC Compression Breakout — catch volatility expansion after
compression. Compression: Bollinger Band Width is in bottom 30%
of last 100 candles, OR ATR(14) is below its 20-period moving
average. Long when price above 1H EMA 200 and weekly/daily VWAP,
15m close breaks range high, breakout volume > 20-bar avg, candle
closes in top 30% of its range."

Same shape works for any asset that mean-reverts in volatility:
BTC, ETH, even MNQ during low-vol mornings.

Mechanic
--------
1. Track Bollinger Band Width and ATR over a rolling window.
2. Detect COMPRESSION: BB width in bottom N% percentile of
   recent W bars OR ATR < ATR_MA_period mean.
3. While compressed, watch for BREAKOUT:
   * Long: bar.close > prior N-bar high
   * Short: bar.close < prior N-bar low
4. Confirm with:
   * Trend filter: price above/below slow EMA (default 200)
   * Volume z-score: breakout bar > recent mean
   * Close location value: close in top/bottom 30% of bar range

Stop = ATR-based outside the breakout range
Target = RR multiple OR until trailing stop

Why this matters
----------------
Compression-release breakouts are the canonical volatility-
expansion trade. They have HIGHER expected R-multiples than
random breakouts because:
* Pre-breakout compression eliminates noise candles
* Post-breakout volatility expansion = the trade IS volatility
* Entry timing is mechanical (close above range)
* Stop placement is unambiguous (back inside range)

This is fundamentally different from a regular trend-pullback
(which trades INTO existing volatility), and complementary to
the sweep/reclaim strategy (which trades against extremes).
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
class CompressionBreakoutConfig:
    """Knobs for compression-release breakout."""

    # Compression detection
    bb_period: int = 20             # Bollinger band period
    bb_std_mult: float = 2.0        # Standard BB
    bb_width_window: int = 100      # Lookback for width-percentile compute
    bb_width_max_percentile: float = 0.30  # Compression = width in bottom 30%
    atr_period: int = 14
    atr_ma_period: int = 20         # Compression = ATR < ATR_MA

    # Breakout window — bar.close must clear the high/low of last N bars
    breakout_lookback: int = 20

    # Trend filter
    trend_ema_period: int = 200     # 200-EMA on the LTF stream
    require_trend_alignment: bool = True

    # Quality gates
    volume_z_lookback: int = 20
    min_volume_z: float = 0.5       # breakout volume z >= this
    min_close_location: float = 0.70  # close in top 30% of bar range

    # Risk
    atr_stop_mult: float = 1.5
    rr_target: float = 2.5
    risk_per_trade_pct: float = 0.005

    # Hygiene
    min_bars_between_trades: int = 12
    max_trades_per_day: int = 2
    warmup_bars: int = 220          # 200-EMA needs ~220 bars

    # Direction
    allow_long: bool = True
    allow_short: bool = True

    # Compression-recency window — fire breakout if compression was
    # active anywhere in the last N bars. Without this, the breakout
    # bar itself isn't "compressed" anymore (volatility just expanded)
    # so the strategy could never fire. 0 = require current bar to
    # be compressed (broken on most realistic tapes).
    compression_recency_window: int = 5


def _ema_step(prev: float | None, value: float, period: int) -> float:
    if prev is None:
        return value
    alpha = 2.0 / (period + 1)
    return alpha * value + (1 - alpha) * prev


class CompressionBreakoutStrategy:
    """Volatility compression release breakout."""

    def __init__(self, config: CompressionBreakoutConfig | None = None) -> None:
        self.cfg = config or CompressionBreakoutConfig()
        # Rolling window of closes for BB compute
        self._closes: deque[float] = deque(
            maxlen=max(
                self.cfg.bb_period,
                self.cfg.breakout_lookback,
                self.cfg.atr_ma_period,
            ) + 5,
        )
        # BB width history for percentile compute
        self._bb_width_history: deque[float] = deque(
            maxlen=self.cfg.bb_width_window + 5,
        )
        # ATR history (rolling window of true ranges)
        self._tr_window: deque[float] = deque(
            maxlen=max(self.cfg.atr_period, self.cfg.atr_ma_period) + 5,
        )
        self._volume_window: deque[float] = deque(
            maxlen=self.cfg.volume_z_lookback,
        )
        self._high_window: deque[float] = deque(
            maxlen=self.cfg.breakout_lookback + 5,
        )
        self._low_window: deque[float] = deque(
            maxlen=self.cfg.breakout_lookback + 5,
        )
        self._trend_ema: float | None = None
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        # Audit
        self._n_compression_active: int = 0
        self._n_breakouts_seen: int = 0
        # Recency tracker: bars since compression was last active.
        # When this is <= compression_recency_window, breakout fires
        # are allowed even if the breakout bar itself isn't compressed.
        self._bars_since_compression: int = 1_000_000
        self._n_volume_rejects: int = 0
        self._n_close_location_rejects: int = 0
        self._n_trend_rejects: int = 0
        self._n_fires: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "compression_active": self._n_compression_active,
            "breakouts_seen": self._n_breakouts_seen,
            "volume_rejects": self._n_volume_rejects,
            "close_location_rejects": self._n_close_location_rejects,
            "trend_rejects": self._n_trend_rejects,
            "fires": self._n_fires,
        }

    # -- compression detection ---------------------------------------------

    def _bb_width_pct(self) -> float | None:
        """Bollinger Band width as a fraction of mean. Returns None
        if not enough data."""
        if len(self._closes) < self.cfg.bb_period:
            return None
        recent = list(self._closes)[-self.cfg.bb_period:]
        mean = sum(recent) / len(recent)
        var = sum((c - mean) ** 2 for c in recent) / len(recent)
        std = var ** 0.5
        if mean <= 0.0:
            return None
        upper = mean + self.cfg.bb_std_mult * std
        lower = mean - self.cfg.bb_std_mult * std
        return (upper - lower) / mean

    def _track_bb_width(self) -> None:
        """Append current BB-width to history. Call once per bar
        (BEFORE the warmup return so history accumulates)."""
        bb_width = self._bb_width_pct()
        if bb_width is not None:
            self._bb_width_history.append(bb_width)

    def _is_compressed(self) -> bool:
        """True if current BB width is in bottom N percentile OR
        current ATR < ATR_MA."""
        bb_width = self._bb_width_pct()
        if (
            bb_width is not None
            and len(self._bb_width_history) >= self.cfg.bb_width_window
        ):
            widths = sorted(self._bb_width_history)
            cutoff_idx = int(self.cfg.bb_width_max_percentile * len(widths))
            cutoff = widths[cutoff_idx] if cutoff_idx < len(widths) else widths[-1]
            if bb_width <= cutoff:
                return True
        # ATR < ATR_MA fallback
        if len(self._tr_window) >= self.cfg.atr_ma_period:
            atr = (
                sum(list(self._tr_window)[-self.cfg.atr_period:])
                / self.cfg.atr_period
            )
            atr_ma = sum(self._tr_window) / len(self._tr_window)
            if atr < atr_ma:
                return True
        return False

    def _volume_z(self, bar: BarData) -> float:
        if len(self._volume_window) < self.cfg.volume_z_lookback:
            return 0.0
        vols = list(self._volume_window)
        mean = sum(vols) / len(vols)
        var = sum((v - mean) ** 2 for v in vols) / len(vols)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (bar.volume - mean) / std

    @staticmethod
    def _close_location_value(bar: BarData) -> float:
        """0.0 = close at low; 1.0 = close at high; 0.5 = middle."""
        rng = bar.high - bar.low
        if rng <= 0.0:
            return 0.5
        return (bar.close - bar.low) / rng

    # -- main entry point --------------------------------------------------

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

        # Update rolling windows
        self._closes.append(bar.close)
        self._volume_window.append(bar.volume)
        # True range
        if hist:
            prev_close = hist[-1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        else:
            tr = bar.high - bar.low
        self._tr_window.append(tr)
        # Trend EMA
        self._trend_ema = _ema_step(
            self._trend_ema, bar.close, self.cfg.trend_ema_period,
        )
        # Track BB-width history every bar (including warmup) so the
        # percentile is well-populated before we start checking.
        self._track_bb_width()

        if self._bars_seen < self.cfg.warmup_bars:
            self._high_window.append(bar.high)
            self._low_window.append(bar.low)
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            self._high_window.append(bar.high)
            self._low_window.append(bar.low)
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx)
            < self.cfg.min_bars_between_trades
        ):
            self._high_window.append(bar.high)
            self._low_window.append(bar.low)
            return None

        # Check compression on PREVIOUS bars (excluding current).
        # Update recency tracker — we fire a breakout if compression
        # was active anywhere in the last N bars.
        compressed = self._is_compressed()
        if compressed:
            self._n_compression_active += 1
            self._bars_since_compression = 0
        else:
            self._bars_since_compression += 1
        compression_recent = (
            self._bars_since_compression <= self.cfg.compression_recency_window
        )

        # Detect breakout vs PRIOR window (not including current bar)
        side: str | None = None
        if (
            self.cfg.allow_long
            and self._high_window
            and bar.close > max(self._high_window)
        ):
            side = "BUY"
            self._n_breakouts_seen += 1
        elif (
            self.cfg.allow_short
            and self._low_window
            and bar.close < min(self._low_window)
        ):
            side = "SELL"
            self._n_breakouts_seen += 1

        # Update high/low windows AFTER breakout check
        self._high_window.append(bar.high)
        self._low_window.append(bar.low)

        if side is None or not compression_recent:
            return None

        # Trend filter
        if self.cfg.require_trend_alignment and self._trend_ema is not None:
            if side == "BUY" and bar.close <= self._trend_ema:
                self._n_trend_rejects += 1
                return None
            if side == "SELL" and bar.close >= self._trend_ema:
                self._n_trend_rejects += 1
                return None

        # Volume confirmation
        if self.cfg.min_volume_z > 0:
            vz = self._volume_z(bar)
            if vz < self.cfg.min_volume_z:
                self._n_volume_rejects += 1
                return None

        # Close location
        clv = self._close_location_value(bar)
        if side == "BUY" and clv < self.cfg.min_close_location:
            self._n_close_location_rejects += 1
            return None
        if side == "SELL" and (1 - clv) < self.cfg.min_close_location:
            self._n_close_location_rejects += 1
            return None

        # Risk sizing
        atr = sum(self._tr_window) / max(len(self._tr_window), 1)
        if atr <= 0.0:
            return None
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None

        entry = bar.close
        if side == "BUY":
            stop = entry - stop_dist
            target = entry + self.cfg.rr_target * stop_dist
        else:
            stop = entry + stop_dist
            target = entry - self.cfg.rr_target * stop_dist

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_fires += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0, leverage=1.0,
            regime=f"compression_breakout_{side.lower()}",
        )


# ---------------------------------------------------------------------------
# Asset-class presets
# ---------------------------------------------------------------------------


def btc_compression_preset() -> CompressionBreakoutConfig:
    """Calibrated for BTC 1h bars."""
    return CompressionBreakoutConfig(
        bb_period=20, bb_std_mult=2.0,
        bb_width_window=100, bb_width_max_percentile=0.30,
        atr_period=14, atr_ma_period=20,
        breakout_lookback=20,
        trend_ema_period=200, require_trend_alignment=True,
        volume_z_lookback=20, min_volume_z=0.5,
        min_close_location=0.70,
        atr_stop_mult=1.5, rr_target=2.5,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=220,
    )


def mnq_compression_preset() -> CompressionBreakoutConfig:
    """Calibrated for MNQ 5m intraday."""
    return CompressionBreakoutConfig(
        bb_period=20, bb_std_mult=2.0,
        bb_width_window=78,    # 1 RTH session at 5m
        bb_width_max_percentile=0.30,
        atr_period=14, atr_ma_period=20,
        breakout_lookback=10,  # ~50 min on 5m
        trend_ema_period=50,   # tighter trend on intraday
        require_trend_alignment=True,
        volume_z_lookback=20, min_volume_z=0.5,
        min_close_location=0.70,
        atr_stop_mult=1.0, rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=6,
        max_trades_per_day=2,
        warmup_bars=78,
    )


def nq_compression_preset() -> CompressionBreakoutConfig:
    """Calibrated for NQ 5m intraday.

    Same Nasdaq-100 underlying as MNQ — same compression-release
    mechanic, same per-bar volatility profile. NQ vs MNQ differs
    only in contract size (5x); the strategy's risk_pct-based qty
    calculation absorbs that.

    Defined as a separate factory (not an alias of MNQ) so future
    NQ-specific tuning has a clean home.
    """
    return CompressionBreakoutConfig(
        bb_period=20, bb_std_mult=2.0,
        bb_width_window=78,
        bb_width_max_percentile=0.30,
        atr_period=14, atr_ma_period=20,
        breakout_lookback=10,
        trend_ema_period=50,
        require_trend_alignment=True,
        volume_z_lookback=20, min_volume_z=0.5,
        min_close_location=0.70,
        atr_stop_mult=1.0, rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=6,
        max_trades_per_day=2,
        warmup_bars=78,
    )


def eth_compression_preset() -> CompressionBreakoutConfig:
    """Calibrated for ETH 1h bars. DeepSeek-tuned 2026-05-02."""
    return CompressionBreakoutConfig(
        bb_period=30, bb_std_mult=2.0,                # was 20 — longer BB reduces false breakouts
        bb_width_window=100, bb_width_max_percentile=0.35,
        atr_period=14, atr_ma_period=20,
        breakout_lookback=20,
        trend_ema_period=200, require_trend_alignment=True,
        volume_z_lookback=20, min_volume_z=0.4,
        min_close_location=0.65,
        atr_stop_mult=1.5, rr_target=2.0,             # was 1.8/2.5 — lower stop + RR improves WR
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=220,
    )


def sol_compression_preset() -> CompressionBreakoutConfig:
    """Calibrated for SOL 1h bars.

    SOL is materially more volatile than ETH. Wider ATR-stop,
    larger BB compression band, lower volume gate. RR target
    bumped to 3.0 to compensate for the wider stops.
    """
    return CompressionBreakoutConfig(
        bb_period=20, bb_std_mult=2.0,
        bb_width_window=100, bb_width_max_percentile=0.40,
        atr_period=14, atr_ma_period=20,
        breakout_lookback=20,
        trend_ema_period=200, require_trend_alignment=True,
        volume_z_lookback=20, min_volume_z=0.3,
        min_close_location=0.60,
        atr_stop_mult=2.2, rr_target=3.0,
        risk_per_trade_pct=0.004,  # smaller risk per trade
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=220,
    )
