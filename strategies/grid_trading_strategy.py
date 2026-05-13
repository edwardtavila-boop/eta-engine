"""
EVOLUTIONARY TRADING ALGO  //  strategies.grid_trading
======================================================
Grid trading — the **primary baseline for crypto perps**.

Why
---
Crypto perpetuals trade 24/7 in long oscillating ranges punctuated
by violent trends. Grid trading is the canonical bot strategy for
that distribution because it monetises the oscillation:

  * Place a ladder of buy levels below a reference price and a
    ladder of sell levels above.
  * On each touch of a buy level, open a long; close at the next
    rung up. Symmetric for shorts.
  * The strategy compounds the *number* of small wins rather than
    chasing a single big move.

This is why Binance, Bybit, OKX all ship grid bots as a first-class
product — it's the most popular and bot-native strategy for perps.

What this implementation IS
---------------------------
A single-position engine-compatible variant. The legacy
``BacktestEngine`` only holds one open position at a time, so a
true multi-rung grid (8 long legs simultaneously open) won't fit
without an inventory layer. To stay protocol-compatible:

  * Compute a rolling reference (median of last ``ref_lookback`` bars).
  * Build N evenly-spaced grid levels around it (``grid_spacing_pct``
    apart).
  * On any bar that touches a grid level *below* the reference and
    we have no position, open LONG; target = next rung up.
    Mirror for shorts.
  * Stop = ``atr_stop_mult * ATR`` outside the grid.

This captures most of the grid edge (mean-reversion entries at
liquid levels) without needing concurrent inventory.

A future ``MultiPositionEngine`` can lift this restriction; the
strategy interface stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class GridConfig:
    """Knobs for grid-trading."""

    ref_lookback: int = 50  # bars used to compute the rolling reference price
    grid_spacing_pct: float = 0.005  # 0.5% between rungs (typical for BTC 1h)
    n_levels: int = 6  # rungs per side (above + below)
    atr_period: int = 14
    atr_stop_mult: float = 2.5  # outside-the-grid stop
    rr_target: float = 1.0  # target = 1 grid step (rr=1 vs grid step)
    risk_per_trade_pct: float = 0.005  # smaller than ORB — many fires
    max_trades_per_day: int = 6
    min_warmup_bars: int = 60
    # Direction filter: when True, only fires LONGs above the
    # rolling reference and SHORTs below — prevents the grid from
    # fading a strong trend into the ground.
    trend_filter: bool = True

    # ---- Adaptive volatility mode (2026-04-27 user mandate) -----------
    # When ``adaptive_volatility`` is True, grid_spacing_pct becomes a
    # FUNCTION of the current ATR percentile rather than a fixed value:
    #   * At low ATR (<= adaptive_atr_pct_min): use ``adaptive_min_spacing_pct``
    #   * At high ATR (>= adaptive_atr_pct_max): use ``adaptive_max_spacing_pct``
    #   * Linearly interpolate between
    # This implements the user spec: "Grid spacing expands when
    # volatility expands and tightens when volatility contracts."
    adaptive_volatility: bool = False
    adaptive_atr_pct_lookback: int = 100
    adaptive_atr_pct_min: float = 0.30  # below this ATR%-rank → min spacing
    adaptive_atr_pct_max: float = 0.70  # above this → max spacing
    adaptive_min_spacing_pct: float = 0.0025  # 0.25%
    adaptive_max_spacing_pct: float = 0.012  # 1.2%
    # Kill switch: when ATR rank > this, disable the grid entirely
    # (volatility regime suggests trending, not grid-friendly).
    adaptive_kill_atr_pct: float = 0.85
    # Range break kill switch — when price closes beyond ref by
    # this multiple of n_levels grid spacing, disable until reset.
    range_break_mult: float = 1.0


@dataclass
class _GridState:
    last_bar_close_date: object = None
    trades_today: int = 0


class GridTradingStrategy:
    """Single-position grid bot. Same protocol as ORBStrategy."""

    def __init__(self, config: GridConfig | None = None) -> None:
        self.cfg = config or GridConfig()
        self._state = _GridState()
        # Audit counters for adaptive mode
        self._n_kill_atr: int = 0
        self._n_kill_range_break: int = 0
        self._n_adaptive_widened: int = 0
        self._n_adaptive_tightened: int = 0

    @property
    def grid_stats(self) -> dict[str, int]:
        return {
            "kill_atr": self._n_kill_atr,
            "kill_range_break": self._n_kill_range_break,
            "adaptive_widened": self._n_adaptive_widened,
            "adaptive_tightened": self._n_adaptive_tightened,
        }

    def _adaptive_spacing_pct(self, hist: list) -> float | None:  # noqa: ANN001
        """Compute volatility-adjusted grid spacing. Returns None when
        the kill switch fires (ATR percentile too high)."""
        if not self.cfg.adaptive_volatility:
            return self.cfg.grid_spacing_pct
        # Need full lookback to compute percentile reliably
        lookback = self.cfg.adaptive_atr_pct_lookback
        if len(hist) < lookback + self.cfg.atr_period + 1:
            return self.cfg.grid_spacing_pct
        # Compute rolling ATR series over the lookback window
        atrs: list[float] = []
        period = self.cfg.atr_period
        for i in range(len(hist) - lookback, len(hist)):
            window = hist[max(0, i - period) : i]
            if not window:
                continue
            atrs.append(sum(b.high - b.low for b in window) / len(window))
        if not atrs:
            return self.cfg.grid_spacing_pct
        current_atr = atrs[-1]
        sorted_atrs = sorted(atrs)
        rank = sum(1 for v in sorted_atrs if v <= current_atr) / len(sorted_atrs)
        # Kill switch
        if rank > self.cfg.adaptive_kill_atr_pct:
            self._n_kill_atr += 1
            return None
        # Linear interpolate between min and max spacing
        lo = self.cfg.adaptive_atr_pct_min
        hi = self.cfg.adaptive_atr_pct_max
        if rank <= lo:
            self._n_adaptive_tightened += 1
            return self.cfg.adaptive_min_spacing_pct
        if rank >= hi:
            self._n_adaptive_widened += 1
            return self.cfg.adaptive_max_spacing_pct
        # Interpolate
        frac = (rank - lo) / max(hi - lo, 1e-9)
        return self.cfg.adaptive_min_spacing_pct + frac * (
            self.cfg.adaptive_max_spacing_pct - self.cfg.adaptive_min_spacing_pct
        )

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        if len(hist) < max(self.cfg.min_warmup_bars, self.cfg.ref_lookback):
            return None

        cur_date = bar.timestamp.date()
        if cur_date != self._state.last_bar_close_date:
            self._state = _GridState(last_bar_close_date=cur_date, trades_today=0)
        if self._state.trades_today >= self.cfg.max_trades_per_day:
            return None

        # Reference = median of recent closes (robust to outlier bars)
        recent = hist[-self.cfg.ref_lookback :]
        sorted_closes = sorted(b.close for b in recent)
        mid = sorted_closes[len(sorted_closes) // 2]
        if mid <= 0.0:
            return None

        # Build grid levels around the reference. When adaptive mode
        # is enabled, spacing scales with volatility regime; the kill
        # switch returns None when ATR rank exceeds threshold.
        spacing_pct = self._adaptive_spacing_pct(hist)
        if spacing_pct is None:
            return None
        spacing = spacing_pct * mid
        if spacing <= 0.0:
            return None
        # Range-break kill switch: if price has decisively closed
        # outside grid range (beyond n_levels rungs), disable for this bar.
        max_dist = self.cfg.range_break_mult * self.cfg.n_levels * spacing
        if abs(bar.close - mid) > max_dist:
            self._n_kill_range_break += 1
            return None
        long_levels = [mid - i * spacing for i in range(1, self.cfg.n_levels + 1)]
        short_levels = [mid + i * spacing for i in range(1, self.cfg.n_levels + 1)]

        # ATR for stop sizing
        atr_bars = hist[-self.cfg.atr_period :]
        atr = sum(b.high - b.low for b in atr_bars) / len(atr_bars) if atr_bars else 0.0
        if atr <= 0.0:
            return None
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None

        # Find the highest long level touched (closest to ref) by
        # this bar's low. Same idea symmetric for shorts.
        long_hit: float | None = None
        for lvl in long_levels:  # ordered from highest (closest to mid) down
            if bar.low <= lvl <= bar.high or bar.close <= lvl:
                long_hit = lvl
                break

        short_hit: float | None = None
        for lvl in short_levels:
            if bar.low <= lvl <= bar.high or bar.close >= lvl:
                short_hit = lvl
                break

        side: str | None = None
        entry_price: float = bar.close
        target: float = bar.close
        # If both fire on the same bar, prefer the one furthest from
        # mid (deeper edge of the grid).
        if long_hit is not None and (short_hit is None or abs(long_hit - mid) >= abs(short_hit - mid)):
            if not self.cfg.trend_filter or bar.close >= mid * (1 - self.cfg.grid_spacing_pct * self.cfg.n_levels):
                side = "BUY"
                entry_price = long_hit
                target = entry_price + self.cfg.rr_target * spacing
        elif short_hit is not None and (
            not self.cfg.trend_filter or bar.close <= mid * (1 + self.cfg.grid_spacing_pct * self.cfg.n_levels)
        ):
            side = "SELL"
            entry_price = short_hit
            target = entry_price - self.cfg.rr_target * spacing

        if side is None:
            return None

        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None
        stop = entry_price - stop_dist if side == "BUY" else entry_price + stop_dist

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
            regime="grid_trading",
        )
        self._state.trades_today += 1
        return opened
