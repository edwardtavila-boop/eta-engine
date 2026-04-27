"""
EVOLUTIONARY TRADING ALGO  //  strategies.adaptive_kelly_sizing
=================================================================
Adaptive Kelly-style position sizing wrapper.

Supercharged sibling of DrawdownAwareSizingStrategy. The previous
DD-sizing was neutral (+4.25 vs +4.28) because:

1. Bar-level equity tracking is too coarse — a brief drawdown
   recovers before the multiplier can act.
2. Only SHRUNK on losses; never amplified on wins. The "ride the
   edge when it's hot" half of Kelly was missing.
3. Linear penalty, not adaptive to win/loss streaks.

This wrapper fixes all three:

* **Trade-level ledger**: tracks last N trade outcomes
  (win/loss + R-multiple). Decisions react to the actual signal-
  bearing events, not bar-by-bar equity wiggles.
* **Bidirectional sizing**: amplifies when last N trades have
  positive average R (edge is hot), shrinks when negative.
* **Adaptive penalty**: gain ramps up on losing streaks, AND on
  high-volatility regimes (when stop-distance scaled by vol is
  expanding, position size shrinks proportionally).

Position-size multiplier formula:

    streak_signal = mean_R(last_N_trades)
    multiplier = base + streak_gain * streak_signal

    Then clipped to [min_size_multiplier, max_size_multiplier].

Defaults are conservative:
* base = 1.0, streak_gain = 0.5
* min_mult = 0.5  (never go below half-size)
* max_mult = 1.3  (never go above 1.3x — avoids ride-the-streak risk)
* streak_window = 5 trades

The wrapper requires the engine to provide trade-close PnL
callbacks. Since the existing engine only passes equity through
maybe_enter(), we approximate trade PnL by tracking equity DELTAS
between maybe_enter() calls — the change since last query is
attributed to the last open trade. Imperfect but tracks the
relevant SIGN.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Protocol

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig, Trade
    from eta_engine.core.data_pipeline import BarData

    class _SubStrategy(Protocol):
        def maybe_enter(
            self,
            bar: BarData,
            hist: list[BarData],
            equity: float,
            config: BacktestConfig,
        ) -> _Open | None:
            ...


@dataclass(frozen=True)
class AdaptiveKellyConfig:
    """Adaptive Kelly sizing knobs."""

    # Streak window — how many recent trade outcomes to consider.
    streak_window: int = 5

    # Base multiplier (when streak is neutral). 1.0 = pass-through.
    base_multiplier: float = 1.0

    # Streak gain — multiplier shifts by streak_gain * mean_R per
    # trade window. mean_R typically ~0.3-1.0 for a real edge.
    # gain=0.5 means at mean_R=1.0, multiplier shifts by +0.5
    # (which is then capped by max_size_multiplier).
    streak_gain: float = 0.5

    # Hard caps (these matter — they prevent ride-the-streak risk
    # and capitulation-cycle dump risk).
    min_size_multiplier: float = 0.5
    max_size_multiplier: float = 1.3

    # Volatility damping. When current ATR / recent-mean ATR > this
    # ratio, additionally cut size by the excess. Default 1.5
    # (when ATR is 1.5x its mean, scale by ~0.67).
    vol_damping_threshold: float = 1.5
    vol_damping_atr_period: int = 14
    vol_damping_lookback: int = 50  # bars for the mean ATR
    vol_damping_enabled: bool = True


class AdaptiveKellySizingStrategy:
    """Trade-level adaptive Kelly sizing wrapper.

    Tracks last N trade outcomes (in R-multiples), shifts position
    size by mean R, with hard caps + volatility damping.
    """

    def __init__(
        self,
        sub_strategy: _SubStrategy,
        config: AdaptiveKellyConfig | None = None,
    ) -> None:
        self._sub = sub_strategy
        self.cfg = config or AdaptiveKellyConfig()
        self._trade_R_history: deque[float] = deque(maxlen=self.cfg.streak_window)
        self._equity_estimate: float | None = None
        self._equity_at_last_open: float | None = None
        self._last_open: _Open | None = None
        # ATR ledger for vol damping
        self._atr_history: deque[float] = deque(maxlen=self.cfg.vol_damping_lookback)
        self._last_atr: float | None = None
        # When True, an upstream engine callback is feeding real trade
        # PnL into ``on_trade_close`` — disable the heuristic equity-
        # delta inference path so we don't double-count.
        self._callback_attached: bool = False
        # Audit counters
        self._n_callback_trades: int = 0
        self._n_inferred_trades: int = 0

    # ─────────────────────────────────────────────────────────────────────
    # Trade-close callback (preferred path)
    # ─────────────────────────────────────────────────────────────────────

    def on_trade_close(self, trade: Trade) -> None:
        """Receive realized trade outcome from the engine.

        This is the CANONICAL signal path — the engine emits the full
        Trade object once per realized exit, and we append the
        observed R-multiple straight into the streak ledger.

        Wiring: caller should pass ``strategy.on_trade_close`` to
        ``BacktestEngine(on_trade_close=...)``. The walk-forward
        harness should do this at construction time when the strategy
        exposes the method.
        """
        self._callback_attached = True
        self._n_callback_trades += 1
        try:
            r = float(trade.pnl_r)
        except (AttributeError, TypeError, ValueError):
            return
        self._trade_R_history.append(r)
        # Clear the inference state — the open is closed by the engine
        # not us. Keeping inference state would risk double-counting.
        self._last_open = None
        self._equity_at_last_open = None

    @property
    def kelly_stats(self) -> dict[str, int | float]:
        """Visibility for walk-forward post-mortems."""
        n = len(self._trade_R_history)
        mean_r = (
            sum(self._trade_R_history) / n if n else 0.0
        )
        return {
            "trade_history_len": n,
            "mean_R": mean_r,
            "callback_attached": int(self._callback_attached),
            "n_callback_trades": self._n_callback_trades,
            "n_inferred_trades": self._n_inferred_trades,
        }

    def _record_close_if_any(self, equity_now: float) -> None:
        """Heuristic fallback when no engine callback is attached.

        If we had an open trade and equity moved, attribute the
        delta to that trade as a realized R-multiple. ONLY fires
        when no engine callback has been received — otherwise we'd
        double-count the same trade.
        """
        if self._callback_attached:
            return
        if self._last_open is None or self._equity_at_last_open is None:
            return
        delta_usd = equity_now - self._equity_at_last_open
        risk = self._last_open.risk_usd
        r_realized = delta_usd / risk if risk > 0 else 0.0  # noqa: N806
        # Heuristic: only treat the open as closed if the equity has
        # shifted by at least ~30% of risk (ignores tiny drift). This
        # avoids spurious "trade closed" detections on neutral bars.
        if abs(delta_usd) >= 0.3 * risk:
            self._trade_R_history.append(r_realized)
            self._n_inferred_trades += 1
            self._last_open = None
            self._equity_at_last_open = None

    def _vol_damping_multiplier(self, hist: list[BarData]) -> float:
        """Return a [0.5, 1.0] multiplier based on current vs mean ATR."""
        if not self.cfg.vol_damping_enabled:
            return 1.0
        recent = hist[-self.cfg.vol_damping_atr_period:] if hist else []
        if len(recent) < 2:
            return 1.0
        atr = sum(b.high - b.low for b in recent) / len(recent)
        if atr <= 0.0:
            return 1.0
        self._atr_history.append(atr)
        self._last_atr = atr
        if len(self._atr_history) < 5:
            return 1.0
        mean_atr = sum(self._atr_history) / len(self._atr_history)
        if mean_atr <= 0.0:
            return 1.0
        ratio = atr / mean_atr
        if ratio <= self.cfg.vol_damping_threshold:
            return 1.0
        # Excess vol damping: at ratio=2.0 with threshold=1.5, damping = 0.75
        damping = self.cfg.vol_damping_threshold / ratio
        return max(0.5, min(1.0, damping))

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Track equity for trade-PnL inference
        if self._equity_estimate is not None:
            self._record_close_if_any(equity)
        self._equity_estimate = equity

        # Compute streak-based multiplier
        streak_mean_r = (
            sum(self._trade_R_history) / len(self._trade_R_history)
            if self._trade_R_history else 0.0
        )
        streak_mult = self.cfg.base_multiplier + self.cfg.streak_gain * streak_mean_r
        streak_mult = max(
            self.cfg.min_size_multiplier,
            min(self.cfg.max_size_multiplier, streak_mult),
        )

        # Compute vol-damping multiplier
        vol_mult = self._vol_damping_multiplier(hist)

        final_mult = streak_mult * vol_mult
        final_mult = max(
            self.cfg.min_size_multiplier,
            min(self.cfg.max_size_multiplier, final_mult),
        )

        # Delegate to sub-strategy
        opened = self._sub.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        # Pass-through if mult ≈ 1.0
        if abs(final_mult - 1.0) < 1e-3:
            self._last_open = opened
            self._equity_at_last_open = equity
            return opened

        # Scale qty + risk_usd
        scaled_qty = opened.qty * final_mult
        scaled_risk = opened.risk_usd * final_mult
        scaled = replace(
            opened,
            qty=scaled_qty,
            risk_usd=scaled_risk,
            regime=(
                f"{opened.regime}_kelly_streakR{streak_mean_r:+.2f}"
                f"_mult{final_mult:.2f}"
            ),
        )
        self._last_open = scaled
        self._equity_at_last_open = equity
        return scaled
