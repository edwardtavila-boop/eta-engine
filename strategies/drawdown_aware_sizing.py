"""
EVOLUTIONARY TRADING ALGO  //  strategies.drawdown_aware_sizing
=================================================================
Drawdown-aware position-size wrapper.

Rationale (Kelly-lite intuition)
--------------------------------
Trade count and per-trade edge are independent of how big you sit
on each trade. By scaling DOWN size after recent losses (when the
edge may be temporarily mis-aligned to current regime) and UP after
wins (when the edge is hot), the SAME trade stream produces a
better risk-adjusted return.

The implementation is intentionally conservative:

* Track equity high-water mark (HWM).
* Compute current drawdown = (HWM - current_equity) / HWM.
* When drawdown > 0, reduce position size by a multiplier:
    multiplier = base * (1 - drawdown_penalty * dd_ratio)
* When at HWM (no drawdown), use full base size.
* Hard cap: multiplier in [min_size_multiplier, 1.0]. Never
  amplifies — only shrinks. Amplifying after wins is a known
  failure mode (ride-the-streak risk).

The wrapper is composable with any underlying strategy. It calls
the underlying ``maybe_enter()`` and then scales the returned
``qty`` and ``risk_usd`` based on the current drawdown read.

Equity tracking
---------------
The wrapper doesn't get equity callbacks from the engine, so it
estimates equity by treating each trade's risk_usd as the realized
equity-delta when the trade closes. This is a rough approximation
but tracks the relevant DIRECTION of equity movement, which is
what the drawdown signal needs.

A more precise version would receive engine-level equity updates
via a callback; for backtest research we stay simple.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Protocol

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData

    class _SubStrategy(Protocol):
        def maybe_enter(
            self,
            bar: BarData,
            hist: list[BarData],
            equity: float,
            config: BacktestConfig,
        ) -> _Open | None: ...


@dataclass(frozen=True)
class DrawdownAwareSizingConfig:
    """Sizing knobs."""

    # Linear penalty: at dd=0 → multiplier=1.0; at dd=1.0 →
    # multiplier=1.0 - drawdown_penalty. With penalty=0.5, at 50%
    # drawdown the multiplier is 0.75 (cut size by 25%).
    drawdown_penalty: float = 0.5

    # Floor — never go below this multiplier even at extreme
    # drawdowns. Default 0.25 = always at least 25% of base size.
    min_size_multiplier: float = 0.25


class DrawdownAwareSizingStrategy:
    """Wraps a sub-strategy and scales size by current drawdown."""

    def __init__(
        self,
        sub_strategy: _SubStrategy,
        config: DrawdownAwareSizingConfig | None = None,
    ) -> None:
        self._sub = sub_strategy
        self.cfg = config or DrawdownAwareSizingConfig()
        self._equity_estimate: float | None = None
        self._high_water: float | None = None

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Track equity high-water mark using the engine's equity arg
        # (the engine passes the current portfolio value)
        if self._equity_estimate is None:
            self._equity_estimate = equity
            self._high_water = equity
        else:
            self._equity_estimate = equity
            if self._high_water is None or equity > self._high_water:
                self._high_water = equity

        # Compute drawdown ratio
        if self._high_water and self._high_water > 0:
            dd = max(0.0, (self._high_water - equity) / self._high_water)
        else:
            dd = 0.0

        # Compute multiplier
        raw = 1.0 - self.cfg.drawdown_penalty * dd
        multiplier = max(self.cfg.min_size_multiplier, min(1.0, raw))

        # Delegate to sub-strategy
        opened = self._sub.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        # If multiplier is essentially 1.0 (no drawdown), return as-is
        if multiplier >= 0.999:
            return opened

        # Scale qty + risk_usd; tag with the dd state
        scaled_qty = opened.qty * multiplier
        scaled_risk = opened.risk_usd * multiplier
        return replace(
            opened,
            qty=scaled_qty,
            risk_usd=scaled_risk,
            regime=f"{opened.regime}_dd{dd:.2f}_mult{multiplier:.2f}",
        )
