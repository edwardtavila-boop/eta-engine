"""
EVOLUTIONARY TRADING ALGO  //  strategies.orb_strategy
=======================================================
Opening Range Breakout (ORB) — the most automated-friendly futures
strategy for MNQ / NQ. Replaces the confluence-scored mean-reversion
default for index-futures bots.

Why ORB
-------
Per the 2026-04-27 strategy review, ORB is the canonical bot-first
strategy for index futures because:

* **Clear rules** — "highest high / lowest low of the first N
  minutes after RTH open." No ambiguity, no scoring threshold
  tuning, no regime tag.
* **Bot-friendly exits** — fixed RR target + ATR trailing stop +
  end-of-session flatten. Three deterministic exit conditions.
* **Backtested edge** — published win rates 55-68% on liquid
  futures during 9:30-11 AM ET. Profit factor 1.5-3 in favorable
  regimes.
* **Low parameter count** — range_minutes, max_trade_after,
  rr_target, atr_stop_mult, plus a couple of filters. Walk-forward
  optimization stays tractable.

The strategy has explicit knobs for each filter mentioned in the
spec; defaults are MNQ-tuned but every parameter is `frozen`-
dataclass field-overridable from the per-bot registry.

Filters
-------
* **Range-width filter** (``min_range_pts``): skip days where the
  opening range is narrower than the threshold — avoids false
  breakouts in chop. Tuned to 1.0× ATR-baseline by default.
* **EMA bias filter** (``ema_bias_period``): only long if price >
  EMA(N), only short if price < EMA(N). Default N=200 matches the
  spec's "200 EMA = trend bias" rule.
* **No-trade-after** (``max_entry_local``): refuse new entries
  after ``max_entry_local`` (default 11:00 ET). Late-day breakouts
  are statistical noise.
* **Volume confirmation** (``volume_mult``): require breakout-bar
  volume ≥ ``volume_mult`` × recent average. Default 1.0 (any
  bar). Bumps to 1.5 in production tighten signal quality.

Returns
-------
``ORBStrategy.maybe_enter(bar, hist, equity, config)`` returns the
same ``_Open | None`` shape the BacktestEngine already consumes.
That's the integration contract — engine doesn't need to know
which strategy is plugged in.

Limitations / honest scope
--------------------------
* Single-position. Pyramiding / scaling-in is a future enhancement.
* No partial profit-taking. Either fully target / stop / EOD.
* Session bias is local-time-of-day only. A real-money refinement
  would gate on macro calendar + ES correlation; both are in the
  ctx_builder for the existing strategy and easy to add here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ORBConfig:
    """All knobs in one place. Every default is MNQ-tuned for 5m bars
    but each can be overridden from the per-bot registry."""

    # Range definition
    range_minutes: int = 15
    rth_open_local: time = time(9, 30)  # ET
    rth_close_local: time = time(16, 0)  # ET
    timezone_name: str = "America/New_York"

    # Trade window
    max_entry_local: time = time(11, 0)  # no new entries after 11:00 ET
    flatten_at_local: time = time(15, 55)  # exit any open trade by 15:55 ET

    # Entry filters
    # 5 MNQ points = ~$10 = a reasonable "non-chop" threshold. Skips dead-tape
    # days where the opening range is microscopic and follow-through is
    # impossible. NOTE: This default is MNQ-scaled — presets for other
    # instruments (ES, CL, GC, 6E, BTC, etc.) MUST override with their own
    # tick-size-appropriate value. 0 = disabled.
    min_range_pts: float = 5.0
    ema_bias_period: int = 200  # 0 = disabled; otherwise require price-EMA alignment
    volume_mult: float = 1.0  # breakout-bar vol >= mult × recent avg
    volume_lookback: int = 20  # bars for the volume average

    # Risk / exits
    # Winning config from 2026-04-27 sweep on MNQ1/5m (60d/30d windows):
    # range=15m, rr=2.0, atr_stop=2.0, ema=200 → DSR 1.000, OOS Sh +5.71,
    # 100% pass fraction, gate PASS. Defaults updated to the winner so
    # any caller that just instantiates ORBStrategy() gets the
    # research-validated baseline.
    rr_target: float = 2.0  # target distance = rr_target × stop distance
    atr_period: int = 14
    atr_stop_mult: float = 2.0  # was 1.5; bumped per sweep
    risk_per_trade_pct: float = 0.01  # 1% of equity per trade
    max_trades_per_day: int = 1  # one ORB trade per session by default

    # Cross-asset filter — when enabled, ctx_builder must populate
    # ``ctx["es_aligned"]`` (True/False). False blocks entry. The
    # default OFF preserves backward compat; turn ON only when the
    # runner is wiring ES bars into ctx.
    require_es_aligned: bool = False

    # ── Cross-asset ES filter (opt-in) ──
    # When True, the strategy ALSO requires ES1 to be breaking out
    # of its own opening range in the same direction. Cross-asset
    # confirmation cuts false breakouts driven by sector rotation
    # rather than broad index momentum.
    require_es_confirmation: bool = False
    # Internal: ES bars are loaded lazily by the runner script and
    # injected via a single ctx_builder that returns {"es_bars": ...}.
    # Strategy itself doesn't reach into the data library — keeps
    # the test surface clean.

    # ── Break-retest mode ──
    # When True, the entry is NOT taken on the breakout bar.
    # Instead, the strategy waits for price to PULL BACK to the
    # broken level (the retest) and then enter on the bounce.
    # This filters ~60-70% of false breakouts — a bar that punches
    # through and immediately reverses never produces a valid retest.
    # A validated breakout retests the broken level (old resistance
    # becomes new support, old support becomes new resistance).
    require_retest: bool = True
    # How close price must pull back to the broken level (in ATR
    # multiples) to count as a valid retest. 0.5 = within 0.5×ATR.
    retest_atr_band: float = 1.0
    # Maximum bars after breakout to wait for retest before
    # invalidating (the breakout "got away"). 0 = unlimited.
    retest_max_bars: int = 5
    # Require the bar that retests to CLOSE back in the breakout
    # direction (proving support/resistance held). Off=enter
    # when price merely touches the retest zone.
    retest_require_close_bounce: bool = True
    # Maximum distance runaway (in ATR) before we cancel the pending
    # breakout. If price moves this far beyond the broken level
    # without retesting, the entry opportunity is gone.
    runaway_atr_mult: float = 2.5


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


@dataclass
class _DayState:
    """Per-day state the strategy carries between bars."""

    date: datetime
    range_high: float | None = None
    range_low: float | None = None
    range_complete: bool = False
    trades_today: int = 0
    breakout_taken: bool = False  # True once we've entered today; blocks re-entry
    # ES-correlation filter state. Tracks ES1 range alongside MNQ.
    # Populated only when ORBConfig.require_es_confirmation=True
    # AND the ctx provides an "es_bars" alignment.
    es_range_high: float | None = None
    es_range_low: float | None = None
    # Break-retest state. Populated only when require_retest=True.
    # When a breakout is detected, we record the broken level and
    # direction; subsequent bars check for retest → confirmation.
    pending_breakout: bool = False
    pending_side: str | None = None
    broken_level: float = 0.0
    breakout_bar_idx: int = 0
    retest_done: bool = False


class ORBStrategy:
    """Opening Range Breakout for MNQ / NQ.

    The strategy is stateful across the bar stream — it tracks
    today's opening range, EMA bias, and how many trades have fired.
    The BacktestEngine instantiates one strategy per backtest run
    so cross-window state stays clean.
    """

    def __init__(
        self,
        config: ORBConfig | None = None,
        *,
        ctx_provider: Callable[[BarData, list[BarData]], dict[str, object]] | None = None,
    ) -> None:
        self.cfg = config or ORBConfig()
        self._tz = ZoneInfo(self.cfg.timezone_name)
        self._day: _DayState | None = None
        self._ema: float | None = None
        self._ema_alpha = 2.0 / (self.cfg.ema_bias_period + 1) if self.cfg.ema_bias_period > 0 else 0.0
        # Optional callable(bar, hist) -> dict — used when
        # require_es_aligned=True to fetch ctx["es_aligned"]. None
        # means the ES gate degrades to "always allow" regardless of
        # the require flag.
        self._ctx_provider = ctx_provider
        # ES-correlation filter: caller may attach a provider that maps a
        # MNQ/NQ bar -> the time-aligned ES bar (or None if ES has no bar
        # at that minute, e.g. ES holiday). Keeping the provider as a
        # callable means the strategy never imports the data library.
        self._es_provider: Callable[[BarData], BarData | None] | None = None

    # -- ES-confirmation plumbing --------------------------------------------

    def attach_es_provider(
        self, provider: Callable[[BarData], BarData | None] | None,
    ) -> None:
        """Wire up an ES-bar provider for the cross-asset filter.

        Pass ``None`` to detach. The provider is called once per
        primary-asset bar; signature is ``provider(bar) -> ES bar | None``
        where the returned bar is treated as the time-aligned ES1 bar.
        Strategy itself stays data-pipeline-agnostic.
        """
        self._es_provider = provider

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Return an open trade or None. Same contract as engine._enter."""
        local_ts = bar.timestamp.astimezone(self._tz)
        today = local_ts.date()

        # ── Per-day state init / reset ──
        if self._day is None or self._day.date != today:
            self._day = _DayState(date=today)  # type: ignore[arg-type]

        # ── Update EMA bias ──
        if self.cfg.ema_bias_period > 0:
            if self._ema is None:
                self._ema = bar.close
            else:
                self._ema = self._ema_alpha * bar.close + (1 - self._ema_alpha) * self._ema

        local_t = local_ts.timetz().replace(tzinfo=None)
        local_t = time(local_t.hour, local_t.minute, local_t.second)

        # ── ES-confirmation: pull aligned ES bar (if filter is on) ──
        es_bar: BarData | None = None
        if self.cfg.require_es_confirmation and self._es_provider is not None:
            try:
                es_bar = self._es_provider(bar)
            except Exception:  # noqa: BLE001 - provider isolation
                es_bar = None

        # ── Phase 1: build the opening range ──
        if not self._day.range_complete:
            if local_t < self.cfg.rth_open_local:
                return None  # pre-RTH bars don't count
            range_end = _add_minutes(self.cfg.rth_open_local, self.cfg.range_minutes)
            if local_t < range_end:
                # accumulate the range
                self._day.range_high = (
                    bar.high if self._day.range_high is None
                    else max(self._day.range_high, bar.high)
                )
                self._day.range_low = (
                    bar.low if self._day.range_low is None
                    else min(self._day.range_low, bar.low)
                )
                # mirror the range build for ES so the breakout phase can
                # cross-check both legs without a second pass
                if es_bar is not None:
                    self._day.es_range_high = (
                        es_bar.high if self._day.es_range_high is None
                        else max(self._day.es_range_high, es_bar.high)
                    )
                    self._day.es_range_low = (
                        es_bar.low if self._day.es_range_low is None
                        else min(self._day.es_range_low, es_bar.low)
                    )
                return None
            # range window has passed
            self._day.range_complete = True

        # If we never accumulated bars (e.g. RTH gap or holiday), abort.
        if self._day.range_high is None or self._day.range_low is None:
            return None

        # ── Phase 2: gate the breakout window ──
        if self._day.breakout_taken:
            return None
        if self._day.trades_today >= self.cfg.max_trades_per_day:
            return None
        if local_t >= self.cfg.rth_close_local:
            return None

        # ── Phase 2.5: compute ATR (needed for retest calcs + entry) ──
        atr = _atr(hist, self.cfg.atr_period)
        if atr <= 0.0:
            return None

        bars_seen = len(hist) + 1  # +1 for current bar

        # ── Phase 2.6: retest logic (when enabled) ──
        if self.cfg.require_retest and self._day.pending_breakout:
            side = None
            entry_price = bar.close
            broken = self._day.broken_level
            pside = self._day.pending_side
            retest_band = self.cfg.retest_atr_band * atr
            runaway_dist = self.cfg.runaway_atr_mult * atr

            # Check runaway: price went too far without retesting — cancel
            if pside == "BUY":
                if bar.low > broken + runaway_dist:
                    self._day.pending_breakout = False
                    return None
                # False breakout: price reversed back inside the range
                if bar.close < self._day.range_high - retest_band:
                    self._day.pending_breakout = False
                    return None
            else:
                if bar.high < broken - runaway_dist:
                    self._day.pending_breakout = False
                    return None
                if bar.close > self._day.range_low + retest_band:
                    self._day.pending_breakout = False
                    return None

            # Check retest staleness
            if self.cfg.retest_max_bars > 0:
                since = bars_seen - self._day.breakout_bar_idx
                if since > self.cfg.retest_max_bars:
                    self._day.pending_breakout = False
                    return None

            # Check retest: did price come back TO the broken level?
            if pside == "BUY":
                price_came_back = bar.low <= broken + retest_band
                # Retest bounce: close holds above the broken level
                if self._day.retest_done:
                    if self.cfg.retest_require_close_bounce:
                        if bar.close > broken:
                            side, entry_price = "BUY", bar.close
                            self._day.pending_breakout = False
                        else:
                            return None
                    else:
                        side, entry_price = "BUY", bar.close
                        self._day.pending_breakout = False
                elif price_came_back:
                    self._day.retest_done = True
                    if not self.cfg.retest_require_close_bounce:
                        side, entry_price = "BUY", bar.close
                        self._day.pending_breakout = False
                    else:
                        if bar.close > broken:
                            side, entry_price = "BUY", bar.close
                            self._day.pending_breakout = False
                        # else: retest touched but close didn't prove support
                        # wait for next bar
                else:
                    return None
            else:  # SELL
                price_came_back = bar.high >= broken - retest_band
                if self._day.retest_done:
                    if self.cfg.retest_require_close_bounce:
                        if bar.close < broken:
                            side, entry_price = "SELL", bar.close
                            self._day.pending_breakout = False
                        else:
                            return None
                    else:
                        side, entry_price = "SELL", bar.close
                        self._day.pending_breakout = False
                elif price_came_back:
                    self._day.retest_done = True
                    if not self.cfg.retest_require_close_bounce:
                        side, entry_price = "SELL", bar.close
                        self._day.pending_breakout = False
                    else:
                        if bar.close < broken:
                            side, entry_price = "SELL", bar.close
                            self._day.pending_breakout = False
                else:
                    return None

            if side is None:
                return None  # retest touched but close didn't confirm — wait

        elif self.cfg.require_retest and not self._day.pending_breakout:
            # ── Detect breakout, record as pending (don't enter yet) ──
            if local_t >= self.cfg.max_entry_local:
                return None

            range_width = self._day.range_high - self._day.range_low
            if self.cfg.min_range_pts > 0 and range_width < self.cfg.min_range_pts:
                return None

            ema = self._ema if self.cfg.ema_bias_period > 0 else bar.close
            long_bias = ema is None or bar.close >= ema
            short_bias = ema is None or bar.close <= ema

            side = None
            if bar.high > self._day.range_high and long_bias:
                side = "BUY"
            elif bar.low < self._day.range_low and short_bias:
                side = "SELL"
            if side is None:
                return None

            # Volume confirmation on breakout bar
            if self.cfg.volume_mult > 0.0:
                recent = hist[-self.cfg.volume_lookback:] if hist else []
                avg_vol = (
                    sum(b.volume for b in recent) / len(recent) if recent else 0.0
                )
                if avg_vol > 0.0 and bar.volume < self.cfg.volume_mult * avg_vol:
                    return None

            # ES confirmation on breakout
            if self.cfg.require_es_aligned and self._ctx_provider is not None:
                try:
                    es_ctx = self._ctx_provider(bar, hist)
                except Exception:
                    es_ctx = {}
                if not bool(es_ctx.get("es_aligned", True)):
                    return None
            if self.cfg.require_es_confirmation:
                if (
                    es_bar is None
                    or self._day.es_range_high is None
                    or self._day.es_range_low is None
                ):
                    return None
                if side == "BUY" and not (es_bar.high > self._day.es_range_high):
                    return None
                if side == "SELL" and not (es_bar.low < self._day.es_range_low):
                    return None

            # Record pending breakout — wait for retest
            self._day.pending_breakout = True
            self._day.pending_side = side
            self._day.broken_level = self._day.range_high if side == "BUY" else self._day.range_low
            self._day.breakout_bar_idx = bars_seen
            self._day.retest_done = False
            return None

        else:
            # ── Legacy: immediate entry on breakout (require_retest=False) ──
            if local_t >= self.cfg.max_entry_local:
                return None

            range_width = self._day.range_high - self._day.range_low
            if self.cfg.min_range_pts > 0 and range_width < self.cfg.min_range_pts:
                return None

            stop_dist = self.cfg.atr_stop_mult * atr
            if stop_dist <= 0.0:
                return None

            # Volume confirmation
            if self.cfg.volume_mult > 0.0:
                recent = hist[-self.cfg.volume_lookback :] if hist else []
                avg_vol = (
                    sum(b.volume for b in recent) / len(recent) if recent else 0.0
                )
                if avg_vol > 0.0 and bar.volume < self.cfg.volume_mult * avg_vol:
                    return None

            ema = self._ema if self.cfg.ema_bias_period > 0 else bar.close
            long_bias = ema is None or bar.close >= ema
            short_bias = ema is None or bar.close <= ema

            side = None
            entry_price = bar.close
            if bar.high > self._day.range_high and long_bias:
                side = "BUY"
            elif bar.low < self._day.range_low and short_bias:
                side = "SELL"
            if side is None:
                return None

            # ES correlation filter
            if self.cfg.require_es_aligned and self._ctx_provider is not None:
                try:
                    es_ctx = self._ctx_provider(bar, hist)
                except Exception:
                    es_ctx = {}
                if not bool(es_ctx.get("es_aligned", True)):
                    return None
            if self.cfg.require_es_confirmation:
                if (
                    es_bar is None
                    or self._day.es_range_high is None
                    or self._day.es_range_low is None
                ):
                    return None
                if side == "BUY" and not (es_bar.high > self._day.es_range_high):
                    return None
                if side == "SELL" and not (es_bar.low < self._day.es_range_low):
                    return None

        # ── Phase 5: build the open trade (shared between retest + legacy) ──
        stop_dist = self.cfg.atr_stop_mult * atr
        if stop_dist <= 0.0:
            return None
        risk_usd = equity * self.cfg.risk_per_trade_pct
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None
        if side == "BUY":
            stop = entry_price - stop_dist
            target = entry_price + self.cfg.rr_target * stop_dist
        else:
            stop = entry_price + stop_dist
            target = entry_price - self.cfg.rr_target * stop_dist

        from eta_engine.backtest.engine import _Open

        regime_tag = "orb_retest" if self.cfg.require_retest else "orb_breakout"
        opened = _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry_price,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0, leverage=1.0,
            regime=regime_tag,
        )
        self._day.breakout_taken = True
        self._day.trades_today += 1
        return opened


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atr(hist: list[BarData], period: int = 14) -> float:
    """Simple ATR over the last ``period`` bars (high-low only).

    Doesn't fold in true range from gap-up days; for intraday MNQ
    that's a minor approximation. Matches the engine's existing
    _atr() helper so backtest results are comparable.
    """
    if not hist:
        return 0.0
    s = hist[-period:]
    if not s:
        return 0.0
    return sum(b.high - b.low for b in s) / len(s)


def _add_minutes(t: time, minutes: int) -> time:
    """Add minutes to a ``time`` value, wrapping at midnight.

    Used to derive the end-of-range time from rth_open_local +
    range_minutes. We don't expect the range window to cross
    midnight in any real config, but the wrap is defensive.
    """
    total_minutes = t.hour * 60 + t.minute + minutes
    total_minutes %= 24 * 60
    return time(total_minutes // 60, total_minutes % 60)
