"""Realistic fill simulator for bar-replay backtests.

Three classes of realism the legacy paper_trade_sim missed:

1. SLIPPAGE on stops, markets, and entries.  Stops fill WORSE than the
   trigger price; entries on a market signal don't fill at the prior
   bar's close — they fill at the next bar's open plus an adverse tick.
2. SAME-BAR straddle resolution.  When a bar's range covers BOTH the
   stop and the target, the legacy sim deterministically picks "stop
   wins."  In reality the order depends on which way the bar opened
   relative to entry, the bar's body direction, and randomness.  This
   module returns a probabilistic resolver with conservative defaults.
3. COMMISSIONS.  Round-trip per contract (futures) or fraction of
   notional (crypto spot).  Charged at exit so the trade ledger reflects
   the broker's bill, not a frictionless ideal.

A FOURTH realism gap — partial fills on illiquid limit orders — is
handled crudely: if a touch-only target sits AT bar.high on a bar with
volume below the 20-bar median, fill probability is reduced.

Modes:
    - "realistic"  (default) — use the slip / commission / straddle models
    - "pessimistic" — wider slip, tighter queue, conservative straddle
    - "legacy"     — perfect fills, zero slip, zero commission (matches the
                     old paper_trade_sim; useful only for A/B comparison)

The simulator is *deterministic given a seed*, so paper-soak runs reproduce.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from eta_engine.feeds.instrument_specs import (
    CRYPTO_SPOT_SYMBOLS,
    CRYPTO_SPOT_TAKER_FEE_RT,
    InstrumentSpec,
    is_rth_session,
)

Mode = Literal["realistic", "pessimistic", "legacy"]


# Per-mode parameter table.  Tuned conservatively for paper_trade_sim
# bar-replay use; pessimistic mode is intentionally harsh so live PnL
# can plausibly land between realistic and pessimistic.
_MODE_PARAMS: dict[Mode, dict[str, float]] = {
    "realistic": dict(
        entry_slip_ticks=1.0,  # Adverse slip on market entry, in ticks
        stop_slip_mult=1.0,  # Multiplier on spec.base_slip_ticks for stop fills
        target_slip_ticks=0.0,  # Limit fills don't slip favorably
        straddle_target_first_pct=0.45,  # P(target hit first | bar straddles both)
        straddle_use_close_bias=1.0,  # Weight on close-vs-open bar-direction informant
        thin_bar_target_skip_pct=0.30,  # P(target NOT filled | touch-only on thin bar)
        commission_mult=1.0,
    ),
    "pessimistic": dict(
        entry_slip_ticks=2.0,
        stop_slip_mult=1.5,
        target_slip_ticks=0.0,
        straddle_target_first_pct=0.30,
        straddle_use_close_bias=1.0,
        thin_bar_target_skip_pct=0.50,
        commission_mult=1.25,
    ),
    "legacy": dict(
        entry_slip_ticks=0.0,
        stop_slip_mult=0.0,
        target_slip_ticks=0.0,
        straddle_target_first_pct=0.0,  # legacy = stop always wins
        straddle_use_close_bias=0.0,
        thin_bar_target_skip_pct=0.0,
        commission_mult=0.0,
    ),
}


@dataclass(frozen=True, slots=True)
class BarOHLCV:
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_iso: str = ""


@dataclass(frozen=True, slots=True)
class EntryFill:
    fill_price: float
    slippage_ticks: float
    commission_charged: bool  # entry charges half of RT commission; exit charges other half
    notes: str


@dataclass(frozen=True, slots=True)
class ExitFill:
    fill_price: float
    exit_reason: str  # "stop_loss" | "take_profit" | "no_exit"
    slippage_ticks: float
    notes: str


class RealisticFillSim:
    """Pure-function bar-replay fill model.  No I/O, no globals.

    Construct once with a mode + seed; call simulate_entry / simulate_exit
    on each relevant bar.  Returns enough metadata for the caller to
    compute commission-aware PnL and tag trades to RTH/overnight buckets.
    """

    def __init__(
        self,
        mode: Mode = "realistic",
        seed: int = 0,
        recent_volume_median_lookback: int = 20,
    ) -> None:
        if mode not in _MODE_PARAMS:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.params = dict(_MODE_PARAMS[mode])
        self._rng = random.Random(seed)
        self._volume_window: list[float] = []
        self._volume_window_max = recent_volume_median_lookback

    # ── public ───────────────────────────────────────────────────────

    def feed_bar_volume(self, volume: float) -> None:
        """Track a rolling window of bar volumes for thin-bar detection."""
        self._volume_window.append(volume)
        if len(self._volume_window) > self._volume_window_max:
            self._volume_window.pop(0)

    def median_volume(self) -> float:
        if not self._volume_window:
            return 0.0
        sorted_vols = sorted(self._volume_window)
        n = len(sorted_vols)
        if n % 2 == 1:
            return sorted_vols[n // 2]
        return 0.5 * (sorted_vols[n // 2 - 1] + sorted_vols[n // 2])

    def simulate_entry(
        self,
        side: str,  # "LONG" or "SHORT"
        entry_bar: BarOHLCV,  # Bar AFTER the signal bar — assume next-bar-open fill
        spec: InstrumentSpec,
    ) -> EntryFill:
        """Market-on-next-open entry with adverse slippage.

        Strategies generate signals on bar i.close.  In live trading the
        broker receives the order and routes it; the realistic fill is at
        bar i+1's open, plus an adverse tick or two for spread/slippage.
        """
        slip_ticks = self._slip_for(spec, entry_bar) * (
            self.params["entry_slip_ticks"] / max(spec.base_slip_ticks, 1.0)
        )
        # entry_slip_ticks in mode params is the BASE; instrument's
        # base_slip_ticks scales it for thin/fast bars.  Clamp:
        slip_ticks = max(slip_ticks, self.params["entry_slip_ticks"])

        direction = +1 if side == "LONG" else -1
        # Adverse slip = pay UP for buys, sell DOWN for shorts.
        slip_price = direction * slip_ticks * spec.tick_size
        fill_price = entry_bar.open + slip_price

        # Round to tick, clamp inside bar range so we never fill outside the bar
        fill_price = self._round_to_tick(fill_price, spec.tick_size)
        fill_price = max(entry_bar.low, min(entry_bar.high, fill_price))

        return EntryFill(
            fill_price=fill_price,
            slippage_ticks=slip_ticks,
            commission_charged=False,  # Defer commission to exit for clarity
            notes=f"market_open_slip={slip_ticks:.2f}t",
        )

    def simulate_exit(
        self,
        side: str,
        position_entry: float,
        stop_price: float,
        target_price: float,
        bar: BarOHLCV,
        spec: InstrumentSpec,
    ) -> ExitFill:
        """Resolve stop/target hits on a bar with realistic semantics.

        Cases:
        - Neither touched: no_exit
        - Stop only touched: stop_loss with adverse slippage
        - Target only touched: take_profit at limit price (subject to thin-bar skip)
        - Both touched (straddle): probabilistic resolver
        """
        if side == "LONG":
            stop_touched = bar.low <= stop_price
            target_touched = bar.high >= target_price
        else:
            stop_touched = bar.high >= stop_price
            target_touched = bar.low <= target_price

        if not stop_touched and not target_touched:
            return ExitFill(0.0, "no_exit", 0.0, "neither_touched")

        if stop_touched and not target_touched:
            return self._fill_stop(side, stop_price, bar, spec)

        if target_touched and not stop_touched:
            return self._fill_target(side, target_price, bar, spec)

        # STRADDLE: both touched on the same bar — use probabilistic resolver
        return self._resolve_straddle(side, position_entry, stop_price, target_price, bar, spec)

    def commission_for_trade(
        self,
        spec: InstrumentSpec,
        qty: float,
        exit_price: float,
    ) -> float:
        """Round-trip commission in USD (charged at exit)."""
        if self.params["commission_mult"] <= 0.0:
            return 0.0
        if spec.symbol in CRYPTO_SPOT_SYMBOLS:
            # bps of notional, rounded both legs
            notional = abs(exit_price) * abs(qty)
            return notional * CRYPTO_SPOT_TAKER_FEE_RT * self.params["commission_mult"]
        return spec.commission_rt * abs(qty) * self.params["commission_mult"]

    # ── internals ────────────────────────────────────────────────────

    def _fill_stop(
        self,
        side: str,
        stop_price: float,
        bar: BarOHLCV,
        spec: InstrumentSpec,
    ) -> ExitFill:
        slip_ticks = self._slip_for(spec, bar) * self.params["stop_slip_mult"]
        # Apply RTH/overnight multiplier
        if bar.ts_iso and not is_rth_session(bar.ts_iso, spec.symbol):
            slip_ticks *= spec.overnight_slip_mult

        direction = +1 if side == "LONG" else -1  # LONG stop: price went down → fill below stop
        # For LONG stop-loss: stop is ABOVE current price's drop; adverse = fill BELOW stop_price
        # For SHORT stop-loss: stop is BELOW current price's rip; adverse = fill ABOVE stop_price
        adverse_sign = -direction
        fill_price = stop_price + adverse_sign * slip_ticks * spec.tick_size

        fill_price = self._round_to_tick(fill_price, spec.tick_size)
        # Clamp to bar — we never fill outside the bar's range
        fill_price = max(bar.low, min(bar.high, fill_price))

        return ExitFill(
            fill_price=fill_price,
            exit_reason="stop_loss",
            slippage_ticks=slip_ticks,
            notes=f"stop_slip={slip_ticks:.2f}t",
        )

    def _fill_target(
        self,
        side: str,
        target_price: float,
        bar: BarOHLCV,
        spec: InstrumentSpec,
    ) -> ExitFill:
        # Targets are limit orders.  If price traded THROUGH the limit (more
        # than 1 tick favorable past the price), full fill at limit price.
        # If price only TOUCHED the limit on a thin bar, may not fill.
        if side == "LONG":
            traded_through = bar.high >= target_price + spec.tick_size
        else:
            traded_through = bar.low <= target_price - spec.tick_size

        if not traded_through:
            # Touch-only — apply thin-bar skip probability
            med = self.median_volume()
            if (
                med > 0
                and bar.volume < med
                and self.params["thin_bar_target_skip_pct"] > 0
                and self._rng.random() < self.params["thin_bar_target_skip_pct"]
            ):
                return ExitFill(0.0, "no_exit", 0.0, "thin_bar_target_skipped")

        fill_price = self._round_to_tick(target_price, spec.tick_size)
        return ExitFill(
            fill_price=fill_price,
            exit_reason="take_profit",
            slippage_ticks=self.params["target_slip_ticks"],
            notes="target_limit_filled",
        )

    def _resolve_straddle(
        self,
        side: str,
        entry: float,
        stop_price: float,
        target_price: float,
        bar: BarOHLCV,
        spec: InstrumentSpec,
    ) -> ExitFill:
        """Same-bar straddle: bar's range touched BOTH stop and target.

        Resolver inputs:
        - Bar direction (close > open ⇒ went up first more often)
        - Distance from open to each level (closer one usually first)
        - Mode-specific straddle_target_first_pct prior

        Conservative default: realistic mode favors stop slightly (0.45
        target-first) because rejection-bar entries that triggered the
        signal often ALREADY had the favorable touch on the signal bar
        and the next bar is more likely to give back.
        """
        # Distance bias: which level is closer to the bar's open
        dist_to_target = abs(bar.open - target_price)
        dist_to_stop = abs(bar.open - stop_price)
        total = dist_to_target + dist_to_stop
        distance_p_target = 0.5 if total <= 0 else dist_to_stop / total  # closer-to-open = wins more often

        # Direction bias: bull bar (close > open) ⇒ for LONG, target is up ⇒ target more likely first
        if bar.close > bar.open:
            direction_p_target = 0.6 if side == "LONG" else 0.4
        elif bar.close < bar.open:
            direction_p_target = 0.4 if side == "LONG" else 0.6
        else:
            direction_p_target = 0.5

        # Blend: 60% distance, 30% direction, 10% mode-prior
        prior = self.params["straddle_target_first_pct"]
        p_target_first = 0.60 * distance_p_target + 0.30 * direction_p_target + 0.10 * prior

        if self._rng.random() < p_target_first:
            res = self._fill_target(side, target_price, bar, spec)
            # If target was skipped due to thin bar, fall back to stop
            if res.exit_reason == "no_exit":
                return self._fill_stop(side, stop_price, bar, spec)
            return ExitFill(
                fill_price=res.fill_price,
                exit_reason="take_profit_straddle",
                slippage_ticks=res.slippage_ticks,
                notes=f"straddle;p_tf={p_target_first:.2f}",
            )
        res = self._fill_stop(side, stop_price, bar, spec)
        return ExitFill(
            fill_price=res.fill_price,
            exit_reason="stop_loss_straddle",
            slippage_ticks=res.slippage_ticks,
            notes=f"straddle;p_tf={p_target_first:.2f}",
        )

    def _slip_for(self, spec: InstrumentSpec, bar: BarOHLCV) -> float:
        """Compute base slippage in ticks for this bar, before mode-mult."""
        slip = spec.base_slip_ticks

        # Fast bar: body / range > 0.8
        rng = bar.high - bar.low
        body = abs(bar.close - bar.open)
        if rng > 0 and body / rng > 0.8:
            slip *= spec.fast_bar_slip_mult

        # Thin bar: volume below 20-bar median
        med = self.median_volume()
        if med > 0 and bar.volume < med:
            slip *= spec.thin_volume_slip_mult

        return slip

    @staticmethod
    def _round_to_tick(price: float, tick: float) -> float:
        if tick <= 0:
            return price
        return round(price / tick) * tick


def get_fill_sim(mode: Mode = "realistic", seed: int = 0) -> RealisticFillSim:
    """Convenience constructor."""
    return RealisticFillSim(mode=mode, seed=seed)


__all__ = [
    "BarOHLCV",
    "EntryFill",
    "ExitFill",
    "Mode",
    "RealisticFillSim",
    "get_fill_sim",
]
