"""
EVOLUTIONARY TRADING ALGO  //  strategies.volume_profile_strategy
==================================================================
Volume Profile / Value Area mean-reversion strategy.

Uses auction-market theory: POC (Point of Control) is a magnetic
level — price spends ~70% of time inside the value area and tends to
revert to POC when it escapes VAH/VAL.

Mechanic
--------
1. Compute volume profile over lookback bars using bucketed prices.
   POC = highest-volume price, VAH/VAL = 70% value area bounds.
2. LONG entry: price < VAL AND showing rejection candle (lower wick,
   bullish body) AND volume > avg. Enter toward POC.
3. SHORT entry: price > VAH AND showing rejection candle (upper wick,
   bearish body) AND volume > avg. Enter toward POC.
4. Exit via ATR-based stops beyond VAH/VAL and RR targets at POC or
   opposite VA boundary.
5. HVN/LVN from volume profile provide structural stop levels.

This is a gravitational strategy — unlike sweep/reclaim (which chases
liquidity events) or ORB (which chases breakouts), volume profile
trades the natural auction cycle of price returning to fair value.

Designed to be wrapped by ConfluenceScorecardStrategy.

Configurable for asset class
-----------------------------
* MNQ 5m: profile_lookback=200 bars (~2 days RTH), bucket_size=2 pts
* BTC 1h: profile_lookback=168 bars (~7 days), bucket_size=50 pts
* ETH 1h: profile_lookback=168 bars, bucket_size=5 pts
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from eta_engine.core.volume_profile import compute_profile

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class VolumeProfileStrategyConfig:
    profile_lookback: int = 200
    bucket_size: float = 2.0
    value_area_pct: float = 0.70
    min_va_spread_atr_mult: float = 0.5
    min_extreme_distance_atr_mult: float = 0.3
    min_poc_distance_atr_mult: float = 1.0
    max_qty_equity_pct: float = 0.01
    freeze_profile_after_warmup: bool = False

    require_rejection: bool = True
    min_rejection_wick_pct: float = 0.25

    volume_z_lookback: int = 20
    min_volume_z: float = 0.2

    atr_period: int = 14
    atr_stop_mult: float = 1.0
    rr_target: float = 1.5
    risk_per_trade_pct: float = 0.005

    min_bars_between_trades: int = 24
    max_trades_per_day: int = 2
    warmup_bars: int = 200

    allow_long: bool = True
    allow_short: bool = True


class VolumeProfileStrategy:

    def __init__(self, config: VolumeProfileStrategyConfig | None = None) -> None:
        self.cfg = config or VolumeProfileStrategyConfig()
        self._price_vol: dict[float, float] = {}
        self._bar_entries: deque[tuple[float, float]] = deque(maxlen=self.cfg.profile_lookback)
        self._volume_window: deque[float] = deque(maxlen=self.cfg.volume_z_lookback)
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        self._n_long_sig: int = 0
        self._n_short_sig: int = 0
        self._n_vol_reject: int = 0
        self._n_fired: int = 0
        self._n_no_profile: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "long_signals": self._n_long_sig,
            "short_signals": self._n_short_sig,
            "vol_rejects": self._n_vol_reject,
            "entries_fired": self._n_fired,
            "no_profile": self._n_no_profile,
        }

    def _bucket_price(self, price: float) -> float:
        return round(price / self.cfg.bucket_size) * self.cfg.bucket_size

    def _compute_profile(self) -> dict[str, float | list[float]]:
        if not self._price_vol:
            return {}
        sorted_prices = sorted(self._price_vol.keys())
        total = sum(self._price_vol.values())
        poc_price = max(self._price_vol, key=lambda k: self._price_vol[k])
        poc_volume = self._price_vol[poc_price]

        target_vol = total * self.cfg.value_area_pct
        poc_idx = sorted_prices.index(poc_price)
        above_idx = poc_idx
        below_idx = poc_idx
        accumulated = poc_volume

        while accumulated < target_vol and (above_idx < len(sorted_prices) - 1 or below_idx > 0):
            next_above = self._price_vol[sorted_prices[above_idx + 1]] if above_idx < len(sorted_prices) - 1 else -1
            next_below = self._price_vol[sorted_prices[below_idx - 1]] if below_idx > 0 else -1
            if next_above >= next_below:
                above_idx += 1
                accumulated += next_above
            else:
                below_idx -= 1
                accumulated += next_below

        vah = sorted_prices[above_idx]
        val = sorted_prices[below_idx]

        hvn_threshold = poc_volume * 0.80
        lvn_threshold = poc_volume * 0.20
        hvns = sorted([p for p, v in self._price_vol.items() if v >= hvn_threshold])
        lvns = sorted([p for p, v in self._price_vol.items() if v <= lvn_threshold])

        return {
            "poc": poc_price, "vah": vah, "val": val,
            "total_volume": total, "poc_volume": poc_volume,
            "hvns": hvns, "lvns": lvns,
        }

    def _is_rejection(self, bar: BarData, side: str) -> bool:
        if not self.cfg.require_rejection:
            return True
        total_range = max(bar.high - bar.low, 1e-9)
        body = abs(bar.close - bar.open)
        body_ratio = body / total_range
        if side == "BUY":
            lower_wick = min(bar.open, bar.close) - bar.low
            wick_ratio = lower_wick / total_range
            if wick_ratio >= self.cfg.min_rejection_wick_pct and body_ratio > 0.10:
                return True
            if bar.close > bar.open and body_ratio > 0.50:
                return True
        else:
            upper_wick = bar.high - max(bar.open, bar.close)
            wick_ratio = upper_wick / total_range
            if wick_ratio >= self.cfg.min_rejection_wick_pct and body_ratio > 0.10:
                return True
            if bar.close < bar.open and body_ratio > 0.50:
                return True
        return False

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

    def _append_bar_to_profile(self, bucket: float, volume: float) -> None:
        """Append the current bar to the rolling volume-profile window.

        Must be called AFTER the entry decision for this bar — appending
        before would let the strategy see its own bar's contribution to
        the POC/VAH/VAL computation, a one-bar look-ahead that inflated
        paper-soak win rates.
        """
        if self.cfg.freeze_profile_after_warmup and self._bars_seen >= self.cfg.warmup_bars:
            return
        self._bar_entries.append((bucket, volume))
        if len(self._bar_entries) > self._bar_entries.maxlen // 2:
            self._price_vol = {}
            for bp, bv in self._bar_entries:
                self._price_vol[bp] = self._price_vol.get(bp, 0.0) + bv

    def maybe_enter(
        self, bar: BarData, hist: list[BarData],
        equity: float, config: BacktestConfig,
    ) -> _Open | None:
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0

        self._bars_seen += 1
        self._volume_window.append(bar.volume)

        typical = (bar.high + bar.low + bar.close) / 3.0
        bucket = self._bucket_price(typical)

        # try/finally guarantees the bar is recorded AFTER the entry
        # decision — fixes the look-ahead.
        try:
            if self._bars_seen < self.cfg.warmup_bars:
                return None
            if self._trades_today >= self.cfg.max_trades_per_day:
                return None
            if (
                self._last_entry_idx is not None
                and (self._bars_seen - self._last_entry_idx) < self.cfg.min_bars_between_trades
            ):
                return None
            return self._evaluate_entry(bar, hist, equity)
        finally:
            self._append_bar_to_profile(bucket, bar.volume)

    def _evaluate_entry(
        self, bar: BarData, hist: list[BarData], equity: float,
    ) -> _Open | None:
        profile = self._compute_profile()
        if not profile or profile["total_volume"] <= 0:
            self._n_no_profile += 1
            return None

        poc = float(profile["poc"])
        vah = float(profile["vah"])
        val = float(profile["val"])
        if val >= vah:
            self._n_no_profile += 1
            return None

        va_spread = vah - val

        atr_window = hist[-self.cfg.atr_period:] if hist else []
        if len(atr_window) < 2:
            return None
        atr = sum(b.high - b.low for b in atr_window) / len(atr_window)
        if atr <= 0.0:
            return None

        if va_spread <= 0.0 or va_spread < self.cfg.min_va_spread_atr_mult * atr:
            return None

        extreme_dist = self.cfg.min_extreme_distance_atr_mult * atr
        side: str | None = None
        if self.cfg.allow_long and bar.close < val - extreme_dist:
            side = "BUY"
            self._n_long_sig += 1
        elif self.cfg.allow_short and bar.close > vah + extreme_dist:
            side = "SELL"
            self._n_short_sig += 1

        if side is None:
            return None

        poc_distance = abs(bar.close - poc)
        if poc_distance < self.cfg.min_poc_distance_atr_mult * atr:
            return None

        if not self._is_rejection(bar, side):
            return None

        vz = self._volume_z_score(bar)
        if vz < self.cfg.min_volume_z:
            self._n_vol_reject += 1
            return None

        stop_dist = max(self.cfg.atr_stop_mult * atr, atr * 0.25)
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = min(risk_usd / stop_dist, equity * self.cfg.max_qty_equity_pct / stop_dist)
        if qty <= 0.0:
            return None

        entry = bar.close
        if side == "BUY":
            structural_stop = val - atr * 0.5
            atr_stop = entry - stop_dist
            # Pick the stop NEAREST to entry that is still BELOW entry.
            # The legacy max() was wrong: when val > entry (which is
            # routine — value-area magnetism setups enter below VAL with
            # POC further above), structural_stop ends up ABOVE entry
            # and max() picks it, creating an instantly-stoppable LONG
            # whose "stop" is actually a target.  Filter to valid stops
            # first, then take the closest.
            candidate_stops = [s for s in (structural_stop, atr_stop) if s < entry]
            if not candidate_stops:
                # Both candidates were on the wrong side — abort the trade
                return None
            stop = max(candidate_stops)  # max of valid (below-entry) = closest to entry
            stop_dist_actual = entry - stop
            if stop_dist_actual <= 0:
                return None
            # Bug fix 2026-05-05: POC can be very far above entry on a
            # value-area-edge entry → reward/risk > 50 trips validator's
            # rr_absurd ceiling.  Cap natural POC target at MAX_RR
            # (= 2x cfg.rr_target).  Was firing 6 rejections in
            # volume_profile_mnq elite-gate (50% bug rate).
            max_rr = 2.0 * self.cfg.rr_target
            max_reward = max_rr * stop_dist_actual
            target = (
                min(poc, entry + max_reward)
                if poc > entry
                else entry + self.cfg.rr_target * stop_dist_actual
            )
        else:
            structural_stop = vah + atr * 0.5
            atr_stop = entry + stop_dist
            # Symmetric for SHORT: stop must be ABOVE entry.
            candidate_stops = [s for s in (structural_stop, atr_stop) if s > entry]
            if not candidate_stops:
                return None
            stop = min(candidate_stops)  # min of valid (above-entry) = closest to entry
            stop_dist_actual = stop - entry
            if stop_dist_actual <= 0:
                return None
            max_rr = 2.0 * self.cfg.rr_target
            max_reward = max_rr * stop_dist_actual
            target = (
                max(poc, entry - max_reward)
                if poc < entry
                else entry - self.cfg.rr_target * stop_dist_actual
            )

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_fired += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=8.0, leverage=1.0,
            regime=f"vp_{side.lower()}_poc{poc:.1f}",
        )


def mnq_volume_profile_preset() -> VolumeProfileStrategyConfig:
    return VolumeProfileStrategyConfig(
        profile_lookback=1000, bucket_size=2.0,
        min_va_spread_atr_mult=2.0, min_extreme_distance_atr_mult=1.5,
        min_poc_distance_atr_mult=2.0,
        max_qty_equity_pct=0.005,
        freeze_profile_after_warmup=True,
        require_rejection=True, min_rejection_wick_pct=0.25,
        volume_z_lookback=20, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.0, rr_target=1.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=24,
        max_trades_per_day=2, warmup_bars=1000,
    )


def nq_volume_profile_preset() -> VolumeProfileStrategyConfig:
    return VolumeProfileStrategyConfig(
        profile_lookback=1000, bucket_size=10.0,
        min_va_spread_atr_mult=2.0, min_extreme_distance_atr_mult=1.5,
        min_poc_distance_atr_mult=2.0,
        max_qty_equity_pct=0.005,
        freeze_profile_after_warmup=True,
        require_rejection=True, min_rejection_wick_pct=0.25,
        volume_z_lookback=20, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.0, rr_target=1.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=24,
        max_trades_per_day=2, warmup_bars=1000,
    )


def btc_volume_profile_preset() -> VolumeProfileStrategyConfig:
    return VolumeProfileStrategyConfig(
        profile_lookback=500, bucket_size=50.0,
        min_va_spread_atr_mult=2.0, min_extreme_distance_atr_mult=1.5,
        min_poc_distance_atr_mult=2.0,
        max_qty_equity_pct=0.005,
        freeze_profile_after_warmup=True,
        require_rejection=True, min_rejection_wick_pct=0.20,
        volume_z_lookback=24, min_volume_z=0.2,
        atr_period=14, atr_stop_mult=1.5, rr_target=2.0,
        risk_per_trade_pct=0.005, min_bars_between_trades=24,
        max_trades_per_day=2, warmup_bars=500,
    )


def eth_volume_profile_preset() -> VolumeProfileStrategyConfig:
    return VolumeProfileStrategyConfig(
        profile_lookback=500, bucket_size=5.0,
        min_va_spread_atr_mult=2.0, min_extreme_distance_atr_mult=1.5,
        min_poc_distance_atr_mult=2.0,
        max_qty_equity_pct=0.005,
        freeze_profile_after_warmup=True,
        require_rejection=True, min_rejection_wick_pct=0.25,
        volume_z_lookback=20, min_volume_z=0.3,
        atr_period=14, atr_stop_mult=1.0, rr_target=1.5,
        risk_per_trade_pct=0.005, min_bars_between_trades=24,
        max_trades_per_day=2, warmup_bars=1000,
    )


def build_vp_confluence_provider(
    lookback: int = 200, bucket_size: float = 2.0,
) -> object:
    """Build a callable that provides volume-profile confluence signals.

    Returns callable(BarData, list[BarData], str) -> float.
    Result is a bonus score 0.0-1.0 for how strongly volume profile
    levels support the trade direction.
    """
    def _vp_confluence(bar: object, hist: list, side: str) -> float:
        if len(hist) < lookback:
            return 0.0
        window = hist[-lookback:]
        buckets: dict[float, float] = {}
        for b in window:
            typical = (b.high + b.low + b.close) / 3.0
            bp = round(typical / bucket_size) * bucket_size
            buckets[bp] = buckets.get(bp, 0.0) + b.volume

        profile = compute_profile(buckets)
        if profile.total_volume <= 0 or profile.val >= profile.vah:
            return 0.0

        price = bar.close
        bonus = 0.0
        va_range = max(profile.vah - profile.val, 1e-9)

        if side.upper() == "BUY" and price < profile.poc:
            if price > profile.val:
                bonus += 0.3
            elif price < profile.val and abs(price - profile.poc) < va_range * 1.5:
                bonus += 0.5
        elif side.upper() == "SELL" and price > profile.poc:
            if price < profile.vah:
                bonus += 0.3
            elif price > profile.vah and abs(price - profile.poc) < va_range * 1.5:
                bonus += 0.5

        for hvn in profile.hvn_levels:
            if abs(price - hvn) / max(abs(price), 1e-9) < 0.002:
                bonus += 0.2
                break

        return min(bonus, 1.0)

    return _vp_confluence
