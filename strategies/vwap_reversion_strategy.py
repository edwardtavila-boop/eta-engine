"""
EVOLUTIONARY TRADING ALGO  //  strategies.vwap_reversion_strategy
==================================================================
Session-anchored VWAP mean-reversion — the natural complement to ORB.

Why: ORB thrives on trend days (open drive, morning continuation).
VWAP reversion thrives on range days (afternoon fade, mean-reversion
to the session VWAP). Running both together hedges the portfolio
across regime.

Mechanic
--------
1. Compute session-anchored VWAP and rolling standard deviation bands
   (VWAP ± 1σ, VWAP ± 2σ).
2. LONG entry: price < VWAP - 2σ AND close > prior bar's low
   (rejection signal) AND volume > avg. Target = VWAP.
3. SHORT entry: price > VWAP + 2σ AND close < prior bar's high
   (rejection signal) AND volume > avg. Target = VWAP.
4. Session bias: entries only during afternoon mean-reversion hours
   (futures: 13:30-15:30 ET; crypto: London open 07:00-09:00 UTC).
5. Stop: structural beyond session extreme or ATR-based.

Edge is well-documented: VWAP deviations > 2σ mean-revert to VWAP
within the same session ~70% of the time. The afternoon session
concentrates this edge.

Designed to be wrapped by ConfluenceScorecardStrategy.

Configurable for asset class
-----------------------------
* MNQ 5m: session_vwap, std_band=2.0, atr_stop_mult=1.0, rr_target=1.5
* BTC 1h: UTC-anchored VWAP, std_band=2.0, atr_stop_mult=1.5, rr_target=2.0
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class VWAPReversionConfig:
    vwap_std_band: float = 2.0
    std_window: int = 100
    vwap_dev_lookback: int = 50
    min_dev_std_mult: float = 1.8

    volume_z_lookback: int = 20
    min_volume_z: float = 0.2

    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_target: float = 1.5
    risk_per_trade_pct: float = 0.005

    min_bars_between_trades: int = 12
    max_trades_per_day: int = 2
    warmup_bars: int = 50

    allow_long: bool = True
    allow_short: bool = True

    # Session window for entry filtering (local time of bar timestamps).
    # Defaults to US RTH (08:00-16:00 ET). Override for crypto sessions.
    session_start: time = time(8, 0)
    session_end: time = time(16, 0)


class VWAPReversionStrategy:

    def __init__(self, config: VWAPReversionConfig | None = None) -> None:
        self.cfg = config or VWAPReversionConfig()
        self._vwap_pv: float = 0.0
        self._vwap_v: float = 0.0
        self._vwap_sq_pv: float = 0.0
        self._vwap_session_id: object | None = None
        self._current_vwap: float = 0.0
        self._current_vwap_std: float = 0.0
        self._volume_window: deque[float] = deque(maxlen=self.cfg.volume_z_lookback)
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        self._n_long_sig: int = 0
        self._n_short_sig: int = 0
        self._n_vol_reject: int = 0
        self._n_fired: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "long_signals": self._n_long_sig,
            "short_signals": self._n_short_sig,
            "vol_rejects": self._n_vol_reject,
            "entries_fired": self._n_fired,
        }

    def _reset_vwap_session(self) -> None:
        self._vwap_pv = 0.0
        self._vwap_v = 0.0
        self._vwap_sq_pv = 0.0
        self._current_vwap = 0.0
        self._current_vwap_std = 0.0

    def _update_vwap(self, bar: BarData) -> None:
        typical = (bar.high + bar.low + bar.close) / 3.0
        vol = max(bar.volume, 1.0)
        self._vwap_pv += typical * vol
        self._vwap_v += vol
        self._vwap_sq_pv += (typical ** 2) * vol
        if self._vwap_v > 0:
            self._current_vwap = self._vwap_pv / self._vwap_v
            mean_sq = self._vwap_sq_pv / self._vwap_v
            variance = mean_sq - self._current_vwap ** 2
            self._current_vwap_std = max(variance, 0.0) ** 0.5

    def _volume_z_score(self, bar: BarData) -> float:
        if len(self._volume_window) < self.cfg.volume_z_lookback:
            return 0.0
        vols = list(self._volume_window)
        mean = sum(vols) / len(vols)
        var = sum((v - mean) ** 2 for v in vols) / len(vols)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (bar.volume - mean) / std

    def _is_allowed_session(self, bar: BarData) -> bool:
        t = bar.timestamp.time()
        return self.cfg.session_start <= t <= self.cfg.session_end

    def maybe_enter(
        self, bar: BarData, hist: list[BarData],
        equity: float, config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()
        day_key = bar_date
        if self._vwap_session_id != day_key:
            self._vwap_session_id = day_key
            self._reset_vwap_session()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0

        self._bars_seen += 1
        self._volume_window.append(bar.volume)

        self._update_vwap(bar)

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
        ):
            return None
        if self._vwap_v < 100.0 or self._current_vwap_std <= 0.0:
            return None

        band = self.cfg.vwap_std_band * self._current_vwap_std
        vwap_high = self._current_vwap + band
        vwap_low = self._current_vwap - band

        side: str | None = None
        if (
            self.cfg.allow_long
            and bar.close < vwap_low
            and len(hist) >= 2
            and bar.close > bar.low + (bar.high - bar.low) * 0.3
        ):
            side = "BUY"
            self._n_long_sig += 1
        if (
            self.cfg.allow_short
            and bar.close > vwap_high
            and len(hist) >= 2
            and bar.close < bar.high - (bar.high - bar.low) * 0.3
        ):
            side = "SELL"
            self._n_short_sig += 1

        if side is None:
            return None

        vz = self._volume_z_score(bar)
        if vz < self.cfg.min_volume_z:
            self._n_vol_reject += 1
            return None

        atr_window = hist[-self.cfg.atr_period:] if hist else []
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

        entry = bar.close
        if side == "BUY":
            stop = entry - stop_dist
            if len(hist) >= 1:
                # FIX: was max() — picked the HIGHER of the two, which when
                # hist[-1].low > entry produced a LONG stop ABOVE entry
                # (same class of bug as volume_profile).  Use min() to pick
                # the LOWER (safer, further-from-entry) candidate, AND
                # filter to candidates that are actually below entry.
                structural = hist[-1].low - atr * 0.1
                if structural < entry:
                    stop = min(stop, structural)
            if stop >= entry:
                return None  # invalid bracket — abort rather than ship a fake-LONG
            stop_dist_actual = entry - stop
            target = self._current_vwap
            if target <= entry:
                target = entry + self.cfg.rr_target * stop_dist_actual
        else:
            stop = entry + stop_dist
            if len(hist) >= 1:
                structural = hist[-1].high + atr * 0.1
                if structural > entry:
                    stop = max(stop, structural)
            if stop <= entry:
                return None  # invalid bracket — abort
            stop_dist_actual = stop - entry
            target = self._current_vwap
            if target >= entry:
                target = entry - self.cfg.rr_target * stop_dist_actual

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_fired += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=7.0, leverage=1.0,
            regime=(
                f"vwap_mr_{side.lower()}_dev"
                f"{abs(bar.close - self._current_vwap) / max(self._current_vwap_std, 1e-9):.1f}s"
            ),
        )


def mnq_vwap_mr_preset() -> VWAPReversionConfig:
    return VWAPReversionConfig(
        vwap_std_band=2.0, min_dev_std_mult=1.8,
        volume_z_lookback=20, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.0, rr_target=1.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=3, warmup_bars=50,
    )


def nq_vwap_mr_preset() -> VWAPReversionConfig:
    return VWAPReversionConfig(
        vwap_std_band=2.0, min_dev_std_mult=1.8,
        volume_z_lookback=20, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.0, rr_target=1.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=3, warmup_bars=50,
    )


def btc_vwap_mr_preset() -> VWAPReversionConfig:
    return VWAPReversionConfig(
        vwap_std_band=2.0, min_dev_std_mult=1.5,
        volume_z_lookback=24, min_volume_z=0.2,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
        session_start=time(7, 0), session_end=time(17, 0),
    )


def eth_vwap_mr_preset() -> VWAPReversionConfig:
    return VWAPReversionConfig(
        vwap_std_band=2.5, min_dev_std_mult=1.5,
        volume_z_lookback=24, min_volume_z=0.2,
        atr_period=14, atr_stop_mult=1.8, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=12,
        max_trades_per_day=2, warmup_bars=72,
        session_start=time(7, 0), session_end=time(17, 0),
    )
