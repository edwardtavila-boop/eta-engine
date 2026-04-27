"""
EVOLUTIONARY TRADING ALGO  //  strategies.generic_sage_daily_gate
==================================================================
Generic daily-sage directional gate — wraps any sub-strategy.

Generalization of `SageDailyGatedStrategy` (which was hard-coded to
crypto_macro_confluence). The breakthrough on BTC (+6.00 OOS, gate
PASS) came from running sage at the right cadence (daily, not 1h).
That same pattern should lift any sub-strategy whose entry trigger
fires on faster bars than sage's natural granularity.

Targets we want to apply this to:
  * MNQ / NQ:  mnq_orb_sage_v1 / nq_orb_sage_v1 (5m bars)
  * BTC alt:   crypto_orb (UTC-anchored, 1h bars)
  * Future:    any new strategy whose underlying signal is intraday

The wrapper does ONE thing: when the underlying strategy proposes
an entry, look up the most recent daily-sage verdict and veto if
direction disagrees AND conviction is above threshold. Otherwise
fire as proposed.

Design notes
------------
* The wrapper does NOT re-implement sage. The runner pre-computes
  daily verdicts once and attaches a provider callable.
* The wrapper does NOT touch sub-strategy internals — pure
  composition. Any object with a `maybe_enter()` method works.
* The wrapper is BIDIRECTIONAL — sage's 'long' allows BUYs only,
  sage's 'short' allows SELLs only, sage's 'neutral' falls through
  in loose mode (or vetoes in strict mode).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date
    from typing import Protocol

    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData
    from eta_engine.strategies.sage_daily_gated_strategy import SageDailyVerdict

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
class GenericSageDailyGateConfig:
    """Sage gate knobs."""

    min_daily_conviction: float = 0.50
    strict_mode: bool = False  # True: neutral sage vetoes; False: neutral falls through


class GenericSageDailyGateStrategy:
    """Wraps any sub-strategy with a daily-sage directional veto.

    Construction takes the sub-strategy and config; runner attaches
    a daily-verdict provider via ``attach_daily_verdict_provider``.
    """

    def __init__(
        self,
        sub_strategy: _SubStrategy,
        config: GenericSageDailyGateConfig | None = None,
    ) -> None:
        self._sub = sub_strategy
        self.cfg = config or GenericSageDailyGateConfig()
        self._verdict_provider: Callable[[date], SageDailyVerdict] | None = None

    def attach_daily_verdict_provider(
        self, provider: Callable[[date], SageDailyVerdict] | None,
    ) -> None:
        """Attach a daily-sage verdict lookup."""
        self._verdict_provider = provider

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        # Always advance underlying state so its EMAs/cooldowns evolve
        opened = self._sub.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None

        # No provider attached → behave identically to underlying
        if self._verdict_provider is None:
            return opened

        try:
            verdict = self._verdict_provider(bar.timestamp.date())
        except Exception:  # noqa: BLE001 - provider isolation
            return opened

        # Conviction below threshold → sage too uncertain to veto
        if verdict.conviction < self.cfg.min_daily_conviction:
            return opened

        # Direction check
        if self.cfg.strict_mode:
            if verdict.direction == "neutral":
                return None
            ok = (
                (opened.side == "BUY" and verdict.direction == "long")
                or (opened.side == "SELL" and verdict.direction == "short")
            )
        else:
            ok = (
                (opened.side == "BUY" and verdict.direction != "short")
                or (opened.side == "SELL" and verdict.direction != "long")
            )

        if not ok:
            return None

        # Tag with sage daily verdict for audit
        new_tag = (
            f"{opened.regime}_dailysage_{verdict.direction}"
            f"_conv{verdict.conviction:.2f}"
        )
        return replace(opened, regime=new_tag)
