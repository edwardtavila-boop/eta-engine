"""
EVOLUTIONARY TRADING ALGO  //  strategies.mbt_zfade_strategy
==============================================================
MBT (CME Bitcoin Micro Future) -- z-score fade with HTF trend filter.

Origin / Honest naming
----------------------
This file replaces the inferred-but-mislabeled
``mbt_funding_basis_strategy``. The legacy strategy was named for a
basis-decay thesis but, in production today (2026-05-07), no
``basis_provider`` is wired; the live signal is just a rolling-window
z-score on a one-bar log-return proxy. That is **not** basis decay --
it's a generic z-score fade. This file renames the mechanism honestly.

The ``basis_provider`` plumbing is preserved so a future feed (CME BRR
mid vs MBT mid) can swap the proxy back to a real basis reading without
changing the strategy class.

EDA basis (70d MBT 5m, 2026-02-26 -> 2026-05-07, n=49 RTH sessions)
-------------------------------------------------------------------
Z-score on a 24-bar log-return rolling proxy, fwd 4 bars (20 min):
  * z >= +2.5: 150 fires, 54.0% reversal rate, mean fwd -3.0 bps
    -> ~$2.40/contract gross, **~$0.90 net** of $1.50 RT friction.
  * z <= -2.5: 148 fires, 57.4% reversal rate, mean fwd +0.3 bps
    (weak -- the long-fade leg is structurally thinner).
  * z >= +2.0: 366 fires, 54.6% reversal rate, mean fwd -2.6 bps
    (marginal; do NOT trade below 2.5 -- net edge collapses).

Edge is small and fast. Two design choices follow:
  * ``rr_target=1.5`` (not 3.0) because the fade reverts in ~20 min;
    a wider target collects too few hits to clear friction.
  * Time-stop = 4 bars (20 min) -- past that, evidence shows the fade
    has either paid or rolled into the start of a new trend.

Mechanic
--------
For each 5m bar (during RTH 08:30-15:00 CT):
  1. proxy_bps = (close - prev_close) / prev_close * 10000
     (or basis_provider(bar) when wired).
  2. Push to a rolling deque of length ``proxy_lookback`` (24).
  3. z = (proxy - mean) / std over the deque.
  4. If abs(z) < entry_z (2.5): skip.
  5. HTF-trend filter (KEY):
       * Build a synthetic 1h bar = average close over last 12 5m bars.
       * Compute EMA(20) over a series of those synthetic 1h closes.
       * 1h slope = current_ema - prior_ema.
       * Only enter the fade if the 1h slope OPPOSES the z-spike sign.
         (z>=+2.5 and slope<=0 -> SHORT;  z<=-2.5 and slope>=0 -> LONG).
       This filters out "z=2.5 because uptrend just kicked in" cases --
       the EDA showed those continuation z-spikes are responsible for
       most of the loser tail.
  6. Stop = entry +/- 1.0 * atr5m. Target = entry +/- 1.5 * atr5m.
  7. Time-stop: track entry_bar; the strategy maintains
     ``_open_entry_bar_idx`` so a future ``maybe_exit`` hook (or the
     live runner) can force-exit at +``time_stop_bars``. The current
     engine has no maybe_exit hook, so the time-stop information is
     exposed via ``stats["time_stop_armed"]`` for live monitoring.

Risk
----
- 1.0x ATR stop, 1.5R target. RR shaved from the legacy 2.0 to match
  the EDA's ~20-minute reversion window; widening RR collapses the hit
  rate below break-even.
- Tick-quantized exits to MBT's 5.0 USD tick.
- Sizing uses MBT point_value = $0.10/point/contract.

Status
------
research_candidate. The EDA was 70-day in-sample (49 sessions). 49
sessions is NOT walk-forward validation. The HTF-trend filter is a
hypothesis added on top of the raw z-fade -- its incremental edge has
NOT been validated separately and may overfit the in-sample tape.
Walk-forward + Monte Carlo + operator-signed kill criteria gate MUST
clear before any promotion past paper-soak.
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


# MBT contract constants -- duplicated from mbt_funding_basis_strategy
# to keep this file self-contained (no cross-module coupling on the
# legacy strategy that this file is replacing in spirit).
_MBT_TICK_SIZE: float = 5.0
# CME Micro Bitcoin: 0.10 BTC per contract. $1 of price move = $0.10 P&L.
# Sizing math MUST multiply stop_dist by this to compute correct contract count.
_MBT_POINT_VALUE: float = 0.10


@dataclass(frozen=True)
class MBTZFadeConfig:
    """Parameters for the MBT z-score fade.

    Defaults are derived from the 70d EDA (2026-02-26 -> 2026-05-07).
    They are still IN-SAMPLE. Walk-forward validation is the gate
    before any promotion past paper-soak.
    """

    # Proxy / z-score window
    proxy_lookback: int = 24       # 24 5m bars = 2h rolling window
    entry_z: float = 2.5            # EDA: net edge collapses below 2.5
    # Exit z is informational; the engine's exit is stop/target driven.
    exit_z: float = 0.0

    # HTF (1h) trend filter -- KEY improvement vs legacy strategy.
    # synthetic 1h bar = average close over last N 5m bars (default 12 = 1h).
    htf_trend_lookback_5m_bars: int = 12
    # EMA period over the series of synthetic 1h closes.
    htf_ema_period: int = 20
    # When True (default), require the 1h slope to OPPOSE the z-spike.
    require_htf_opposition: bool = True

    # Risk / sizing
    atr_period: int = 14
    atr_stop_mult: float = 1.0      # 1.0x ATR stop
    rr_target: float = 1.5          # EDA: ~20-min reversion -> tight target
    risk_per_trade_pct: float = 0.005

    # Time-stop -- engine has no maybe_exit hook today; the value is
    # tracked in strategy state for the live runner / future hook.
    time_stop_bars: int = 4

    # Hygiene
    min_bars_between_trades: int = 12
    max_trades_per_day: int = 3      # bumped from legacy 2 -- EDA showed
    # 150 fires on z>=+2.5 over 49 sessions = ~3 per session ceiling.
    warmup_bars: int = 50

    # Session gating -- RTH-only. MBT trades 23h but the z-fade's edge
    # is concentrated in liquid US hours where mean-reversion capital
    # is awake.
    rth_open_local: time = time(8, 30)
    rth_close_local: time = time(15, 0)
    timezone_name: str = "America/Chicago"

    # Direction -- both legs enabled. EDA showed the long-fade leg is
    # structurally thinner (mean fwd +0.3 bps vs -3.0 bps for shorts)
    # but operator opted to keep both as research_candidate; if walk-
    # forward shows the long leg is uneconomic, set allow_long=False.
    allow_long: bool = True
    allow_short: bool = True


class MBTZFadeStrategy:
    """Stateful z-score fade with HTF trend confirmation.

    Replaces ``MBTFundingBasisStrategy`` (which fell back to log-return
    z-scoring in the absence of a basis provider -- same mechanism, but
    mislabeled). Direction-symmetric: both gap-up-spike (SHORT) and
    gap-down-spike (LONG) are eligible.

    A ``basis_provider`` callable is preserved as an optional override
    so a future CME-BRR-vs-MBT feed can swap the proxy back to a real
    basis reading without rewriting this class.
    """

    def __init__(
        self,
        config: MBTZFadeConfig | None = None,
        *,
        basis_provider: Callable[[BarData], float | None] | None = None,
    ) -> None:
        self.cfg = config or MBTZFadeConfig()
        self._tz = ZoneInfo(self.cfg.timezone_name)
        # Optional callable: bar -> basis bps reading. None means
        # "use log-return proxy".
        self._basis_provider = basis_provider
        self._proxy_window: deque[float] = deque(
            maxlen=self.cfg.proxy_lookback,
        )
        # Rolling series of synthetic 1h closes -- used for the EMA(20)
        # slope check. We keep enough history to compute two EMAs back-
        # to-back: htf_ema_period + 1 entries minimum.
        self._htf_close_series: deque[float] = deque(
            maxlen=self.cfg.htf_ema_period + 4,
        )
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None
        # Time-stop bookkeeping. The current engine doesn't expose a
        # maybe_exit hook, but the live runner / a future hook will use
        # _open_entry_bar_idx + cfg.time_stop_bars to force-flatten.
        self._open_entry_bar_idx: int | None = None
        # Audit
        self._n_z_triggers: int = 0
        self._n_htf_rejects: int = 0
        self._n_session_rejects: int = 0
        self._n_fired: int = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "bars_seen": self._bars_seen,
            "z_triggers": self._n_z_triggers,
            "htf_rejects": self._n_htf_rejects,
            "session_rejects": self._n_session_rejects,
            "entries_fired": self._n_fired,
            "time_stop_armed": int(self._open_entry_bar_idx is not None),
        }

    # -- helpers ----------------------------------------------------------

    def _proxy_value(self, bar: BarData, hist: list[BarData]) -> float | None:
        """Current proxy reading. Falls back to a one-bar log-return
        scaled to bps when no provider is wired."""
        if self._basis_provider is not None:
            try:
                return self._basis_provider(bar)
            except Exception:  # noqa: BLE001 -- provider isolation
                return None
        if not hist:
            return 0.0
        prev = hist[-1].close
        if prev <= 0.0:
            return 0.0
        return (bar.close - prev) / prev * 10000.0

    def _z_score(self, value: float) -> float:
        if len(self._proxy_window) < self.cfg.proxy_lookback:
            return 0.0
        vals = list(self._proxy_window)
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = var ** 0.5
        if std <= 0.0:
            return 0.0
        return (value - mean) / std

    def _update_htf_series(self, bar: BarData, hist: list[BarData]) -> None:
        """Append a synthetic 1h close to the HTF series.

        Synthetic 1h close = average close of the last
        ``htf_trend_lookback_5m_bars`` bars (current bar inclusive).
        We update once per bar; the EMA slope check below pulls the
        latest two entries.
        """
        n = self.cfg.htf_trend_lookback_5m_bars
        if n <= 0:
            return
        # The current bar is the last input; combine it with hist
        # so the synthetic close uses the most recent N bars.
        window: list[float] = []
        if hist:
            for b in hist[-(n - 1):]:
                window.append(b.close)
        window.append(bar.close)
        if not window:
            return
        synth_close = sum(window) / len(window)
        self._htf_close_series.append(synth_close)

    def _htf_slope(self) -> float | None:
        """Return EMA(period) - EMA_prev(period) over the synthetic
        1h close series, or None if not enough history."""
        period = self.cfg.htf_ema_period
        series = list(self._htf_close_series)
        # Need at least period + 1 closes to compute two EMAs back-to-back.
        if len(series) < period + 1:
            return None
        # Standard EMA: alpha = 2 / (period + 1). Seed with the first
        # `period` simple-mean.
        alpha = 2.0 / (period + 1)
        sma_seed = sum(series[:period]) / period
        ema = sma_seed
        prev_ema = ema
        # Walk forward through closes after the seed window.
        for c in series[period:]:
            prev_ema = ema
            ema = alpha * c + (1.0 - alpha) * ema
        return ema - prev_ema

    def _htf_passes(self, z: float) -> bool:
        """True iff HTF slope opposes the z-spike sign (or filter off).

        z >= +entry_z (overshoot): require slope <= 0 (down/flat 1h).
        z <= -entry_z (undershoot): require slope >= 0 (up/flat 1h).
        Insufficient HTF history -> reject (conservative).
        """
        if not self.cfg.require_htf_opposition:
            return True
        slope = self._htf_slope()
        if slope is None:
            return False
        if z >= self.cfg.entry_z:
            return slope <= 0.0
        if z <= -self.cfg.entry_z:
            return slope >= 0.0
        return False

    def _in_session(self, bar: BarData) -> bool:
        local_t = bar.timestamp.astimezone(self._tz).timetz()
        local_only = time(local_t.hour, local_t.minute, local_t.second)
        return self.cfg.rth_open_local <= local_only < self.cfg.rth_close_local

    @staticmethod
    def _quantize_to_tick(price: float, tick: float) -> float:
        if tick <= 0.0:
            return price
        return round(price / tick) * tick

    def _maybe_clear_time_stop(self) -> None:
        """If a virtual position is open and time-stop has elapsed,
        clear the bookkeeping. The engine itself exits on stop/target
        -- this is purely state hygiene for the future maybe_exit hook
        and for stats reporting."""
        if self._open_entry_bar_idx is None:
            return
        if (self._bars_seen - self._open_entry_bar_idx) >= self.cfg.time_stop_bars:
            self._open_entry_bar_idx = None

    # -- main entry point ------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Day boundary anchored to America/Chicago (CME local).
        bar_date = bar.timestamp.astimezone(self._tz).date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0
            # Day rollover also clears any stale time-stop state.
            self._open_entry_bar_idx = None

        self._bars_seen += 1
        self._maybe_clear_time_stop()

        # Update rolling proxy + HTF series every bar (even pre-warmup)
        # so the windows fill in as soon as possible.
        proxy = self._proxy_value(bar, hist)
        if proxy is not None:
            self._proxy_window.append(proxy)
        self._update_htf_series(bar, hist)

        # --- Eligibility gates ---
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

        # Z-score check -- fire only on |z| >= entry_z.
        z = self._z_score(proxy)
        if abs(z) < self.cfg.entry_z:
            return None
        self._n_z_triggers += 1

        # Direction gates -- direction is determined by z sign:
        # z > 0  (positive return spike)  -> SHORT (fade upside).
        # z < 0  (negative return spike)  -> LONG  (fade downside).
        if z > 0 and not self.cfg.allow_short:
            return None
        if z < 0 and not self.cfg.allow_long:
            return None

        # HTF-trend confirmation -- the structural improvement vs the
        # legacy strategy.
        if not self._htf_passes(z):
            self._n_htf_rejects += 1
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
        # $-per-contract = stop_dist (price points) x point_value
        # Without point_value the strategy would size 10x larger.
        qty = risk_usd / (stop_dist * _MBT_POINT_VALUE)
        if qty <= 0.0:
            return None

        entry = bar.close

        if z > 0:
            side = "SELL"
            raw_stop = entry + stop_dist
            raw_target = entry - self.cfg.rr_target * stop_dist
        else:
            side = "BUY"
            raw_stop = entry - stop_dist
            raw_target = entry + self.cfg.rr_target * stop_dist

        stop = self._quantize_to_tick(raw_stop, _MBT_TICK_SIZE)
        target = self._quantize_to_tick(raw_target, _MBT_TICK_SIZE)
        # Defensive: tick rounding can briefly push stop/target on the
        # wrong side of entry for very small ATRs -- bump out by a tick.
        if side == "BUY":
            if stop >= entry:
                stop = entry - _MBT_TICK_SIZE
            if target <= entry:
                target = entry + _MBT_TICK_SIZE
        else:
            if stop <= entry:
                stop = entry + _MBT_TICK_SIZE
            if target >= entry:
                target = entry - _MBT_TICK_SIZE

        from eta_engine.backtest.engine import _Open

        self._last_entry_idx = self._bars_seen
        self._open_entry_bar_idx = self._bars_seen
        self._trades_today += 1
        self._n_fired += 1
        return _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=8.0, leverage=1.0,
            regime=f"mbt_zfade_{side.lower()}_z{z:.2f}",
        )


# ---------------------------------------------------------------------------
# Preset
# ---------------------------------------------------------------------------


def mbt_zfade_preset() -> MBTZFadeConfig:
    """Default research_candidate config for MBT z-fade.

    Parameters are derived from the 70d EDA (2026-02-26 -> 2026-05-07,
    49 RTH sessions). This is in-sample. Walk-forward + operator-
    signed kill criteria gate MUST clear before promotion past paper-
    soak.
    """
    return MBTZFadeConfig(
        proxy_lookback=24,
        entry_z=2.5,
        atr_period=14,
        atr_stop_mult=1.0,
        rr_target=1.5,
        time_stop_bars=4,
        htf_trend_lookback_5m_bars=12,
        htf_ema_period=20,
        max_trades_per_day=3,
        min_bars_between_trades=12,
        warmup_bars=50,
        risk_per_trade_pct=0.005,
        timezone_name="America/Chicago",
        rth_open_local=time(8, 30),
        rth_close_local=time(15, 0),
    )
