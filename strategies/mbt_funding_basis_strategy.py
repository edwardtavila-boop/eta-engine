"""
EVOLUTIONARY TRADING ALGO  //  strategies.mbt_funding_basis_strategy
=====================================================================
MBT (CME Bitcoin Micro Future) — basis-premium mean reversion.

Concept
-------
CME bitcoin futures are NOT perpetual swaps. They settle to the
CME CF Bitcoin Reference Rate (BRR) at expiry, but during the bulk
of the contract's life they trade at a small premium (contango) or
discount (backwardation) to BTC spot.

When the basis stretches well above its rolling mean — typically
during euphoric retail flow or short-covering — the premium tends
to decay back into the mean over the following hours/days as
arbitrageurs short the future and long spot. We can't trade spot
in this strategy (the bot trades MBT only), so we approximate the
trade by going SHORT MBT when the front-month premium is rich and
the order flow on MBT itself is fading.

Mechanic
--------
1. Track a rolling window of "basis proxy" values. The strategy
   accepts a ``basis_provider`` callable that maps the current
   bar to a basis-in-bps reading.

   **IMPORTANT — current production deploy state (2026-05-07):** no
   `basis_provider` is wired in production today. Without one the
   strategy falls back to ``(close - prev_close) / prev_close`` —
   i.e. a one-bar log return, which is **not** basis. In that
   degraded mode the strategy is operating as a short-side momentum-
   fade z-filter, NOT a basis-decay trade. Walk-forward results
   produced in this state validate a different mechanism than the
   strategy name implies. Wire a real provider (CME BRR vs MBT mid
   feed) before treating any backtest result as a "basis" signal.
2. On each bar compute the z-score of the proxy vs the rolling
   window. When z >= ``entry_z`` AND the most recent N bars are
   showing fading momentum (bearish reversal candle, lower-high
   sequence) -> short the future, target a fade back to the mean.
3. RTH-only. CME futures don't strictly require RTH but the basis
   premium decays cleanest during liquid US hours where arbitrage
   capital is most active.

Risk
----
- 1.0x ATR stop (tighter than spot crypto's 1.5-2.0 because MBT
  micro-tick = $0.50 / contract; PnL granularity is finer and
  we want to keep risk-per-trade in line with the small notional).
- 2.0R target. Mean-reversion edges decay quickly on contracts
  with overnight risk; we don't try to ride the entire fade.
- ATR-aware tick quantization at exit boundaries.

Status
------
research_candidate — parameter values are CONSERVATIVE defaults
chosen for sanity, NOT optimized. Walk-forward validation must
land before this is promoted past paper-soak. See TODO blocks for
the explicit calibration items.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


# MBT contract: tick_size = 5.0 USD price increment. Quantizing
# stops/targets to the tick avoids "phantom-fill" off-grid prices in
# realistic_fill_sim.
_MBT_TICK_SIZE: float = 5.0
# CME Micro Bitcoin: 0.10 BTC per contract. $1 of price move = $0.10 P&L.
# Sizing math MUST multiply stop_dist by this to compute correct contract count.
_MBT_POINT_VALUE: float = 0.10


@dataclass(frozen=True)
class MBTFundingBasisConfig:
    """Parameters for the MBT funding-basis fade.

    Defaults are CONSERVATIVE — they are chosen so the strategy
    passes a smoke test on synthetic data and ships zero trades on
    quiet tape rather than overfitting to a specific sample.
    Walk-forward optimization is the next gate before promotion.
    """

    # Basis proxy window
    # TODO(walk-forward): tune lookback against historical MBT/BTC
    # premium-mean-reversion half-life.
    basis_lookback: int = 24       # ~24 5m bars = 2h, or 24 15m bars = 6h
    entry_z: float = 1.5            # z-score threshold to fade premium
    exit_z: float = 0.0             # exit when premium re-touches the mean

    # Momentum confirmation: fade only if the latest N bars are
    # showing reversal (lower highs / lower closes). Helps avoid
    # fading a basis spike that's part of a multi-bar squeeze.
    momentum_lookback: int = 3
    require_lower_highs: bool = True

    # Risk / sizing
    atr_period: int = 14
    atr_stop_mult: float = 1.0      # tighter than spot crypto
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.005

    # Hygiene
    min_bars_between_trades: int = 12
    max_trades_per_day: int = 2
    warmup_bars: int = 50

    # Session gating — CME crypto futures trade ~24h but liquid
    # window is RTH (08:30-15:00 CT). Basis-fade arbitrage is
    # cleanest when US arbitrage capital is awake.
    rth_open_local: time = time(8, 30)
    rth_close_local: time = time(15, 0)
    timezone_name: str = "America/Chicago"

    # Direction — basis-premium fade is structurally short-only
    # (rich premium decays; deep discount is rarely tradeable on
    # CME because it implies backwardation = bearish-spot regime).
    allow_long: bool = False
    allow_short: bool = True


class MBTFundingBasisStrategy:
    """Single-purpose MBT basis-premium fade.

    Stateful: maintains a rolling basis-proxy window and the last
    few bars for momentum confirmation. The engine instantiates one
    instance per backtest run.
    """

    def __init__(
        self,
        config: MBTFundingBasisConfig | None = None,
        *,
        basis_provider: Callable[[BarData], float | None] | None = None,
    ) -> None:
        self.cfg = config or MBTFundingBasisConfig()
        self._tz = ZoneInfo(self.cfg.timezone_name)
        # Optional callable: bar -> basis bps reading. None means
        # "use log-return proxy".
        self._basis_provider = basis_provider
        self._basis_window: deque[float] = deque(
            maxlen=self.cfg.basis_lookback,
        )
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        # Audit
        self._n_z_triggers: int = 0
        self._n_momentum_rejects: int = 0
        self._n_session_rejects: int = 0
        self._n_fired: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "z_triggers": self._n_z_triggers,
            "momentum_rejects": self._n_momentum_rejects,
            "session_rejects": self._n_session_rejects,
            "entries_fired": self._n_fired,
        }

    # -- helpers ----------------------------------------------------------

    def _basis_proxy(self, bar: BarData, hist: list[BarData]) -> float | None:
        """Return current basis reading. Falls back to a log-return
        proxy when no provider is wired."""
        if self._basis_provider is not None:
            try:
                return self._basis_provider(bar)
            except Exception:  # noqa: BLE001 - provider isolation
                return None
        # Fallback: bar-to-bar log return scaled by 10000 (bps-ish).
        # Not a true basis but co-moves with euphoric flow on MBT.
        if not hist:
            return 0.0
        prev = hist[-1].close
        if prev <= 0.0:
            return 0.0
        return (bar.close - prev) / prev * 10000.0

    def _z_score(self, value: float) -> float:
        if len(self._basis_window) < self.cfg.basis_lookback:
            return 0.0
        vals = list(self._basis_window)
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (value - mean) / std

    def _momentum_fading(self, bar: BarData, hist: list[BarData]) -> bool:
        """True if the recent N bars show lower-highs (bearish)."""
        if not self.cfg.require_lower_highs:
            return True
        n = self.cfg.momentum_lookback
        if len(hist) < n:
            return False
        recent = hist[-n:]
        # Lower-highs sequence: each bar's high <= prior high
        for i in range(1, n):
            if recent[i].high > recent[i - 1].high:
                return False
        # And current bar should also be a non-new-high
        return bar.high <= recent[-1].high

    def _in_session(self, bar: BarData) -> bool:
        local_t = bar.timestamp.astimezone(self._tz).timetz()
        local_only = time(local_t.hour, local_t.minute, local_t.second)
        return self.cfg.rth_open_local <= local_only < self.cfg.rth_close_local

    @staticmethod
    def _quantize_to_tick(price: float, tick: float) -> float:
        """Quantize price to the nearest tick. MBT tick = 5.0 USD."""
        if tick <= 0.0:
            return price
        return round(price / tick) * tick

    # -- main entry point ------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Day boundary anchored to America/Chicago (CME local). UTC-date
        # would split the CME RTH session in winter and merge across
        # sessions in summer — both bugs.
        bar_date = bar.timestamp.astimezone(self._tz).date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0

        self._bars_seen += 1

        proxy = self._basis_proxy(bar, hist)
        if proxy is not None:
            self._basis_window.append(proxy)

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if proxy is None:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx)
            < self.cfg.min_bars_between_trades
        ):
            return None
        if not self._in_session(bar):
            self._n_session_rejects += 1
            return None

        # Z-score check — only short when premium is rich
        z = self._z_score(proxy)
        if z < self.cfg.entry_z:
            return None
        self._n_z_triggers += 1

        # Direction gate
        if not self.cfg.allow_short:
            return None

        # Momentum confirmation: fading premium should coincide
        # with bar-action losing steam.
        if not self._momentum_fading(bar, hist):
            self._n_momentum_rejects += 1
            return None

        # Risk sizing
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
        # qty = $risk / ($-per-contract for stop_dist of price)
        # $-per-contract = stop_dist (price points) × point_value (dollars/point/contract)
        # Without the point_value multiplier MBT would be sized 10x larger than intended.
        qty = risk_usd / (stop_dist * _MBT_POINT_VALUE)
        if qty <= 0.0:
            return None

        entry = bar.close
        # Short trade
        raw_stop = entry + stop_dist
        raw_target = entry - self.cfg.rr_target * stop_dist
        stop = self._quantize_to_tick(raw_stop, _MBT_TICK_SIZE)
        target = self._quantize_to_tick(raw_target, _MBT_TICK_SIZE)
        # Tick rounding can briefly push stop/target on the wrong
        # side of entry for very small ATRs — bump out by one tick
        # to keep the _Open invariant happy.
        if stop <= entry:
            stop = entry + _MBT_TICK_SIZE
        if target >= entry:
            target = entry - _MBT_TICK_SIZE

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        self._n_fired += 1
        return _Open(
            entry_bar=bar, side="SELL", qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=8.0, leverage=1.0,
            regime=f"mbt_basis_fade_z{z:.2f}",
        )


# ---------------------------------------------------------------------------
# Preset
# ---------------------------------------------------------------------------


def mbt_funding_basis_preset() -> MBTFundingBasisConfig:
    """Default research_candidate config for MBT basis fade.

    NOTE: parameters are CONSERVATIVE sanity defaults. Walk-forward
    validation against MBT/BTC paired data is required before any
    promotion to live.
    """
    return MBTFundingBasisConfig(
        basis_lookback=24,
        entry_z=1.5,
        exit_z=0.0,
        momentum_lookback=3,
        require_lower_highs=True,
        atr_period=14,
        atr_stop_mult=1.0,
        rr_target=2.0,
        risk_per_trade_pct=0.005,
        min_bars_between_trades=12,
        max_trades_per_day=2,
        warmup_bars=50,
    )
