"""
EVOLUTIONARY TRADING ALGO  //  strategies.sage_gated_orb_strategy
==================================================================
ORB with a sage-consensus overlay on the breakout direction.

Why
---
The 2026-04-27 sweep promoted ``mnq_orb_v1`` on agg OOS Sharpe
+0.80 / DSR median 0.52. Solid but not spectacular. The dominant
loss mode in real MNQ tape is FALSE BREAKOUTS — a 5m bar punches
through the opening range, fires the entry, then immediately
reverses.

Sage gives us a cheap second opinion. When all 22 market-theory
schools collectively say "NO this is not a real breakout" — i.e.
their composite_bias is NEUTRAL or opposite the breakout side —
the overlay vetoes the entry. When sage agrees with the breakout
direction, the trade fires as before.

The overlay is OPT-IN per ORBConfig (see ``sage_overlay_enabled``)
and uses the same SageConsensusConfig knobs as the standalone
sage strategy. Operators tune the threshold and the strategy stays
ORB-the-strategy-they-promoted, just with a quality filter.

Composition
-----------
This is a *composition* strategy: it embeds an ORBStrategy and
delegates to it. When ORB returns an _Open, we run sage; if sage
disagrees, we reject by returning None and rolling back the ORB
day-state so the strategy gets to fire later in the day if a
better breakout appears.

Test surface stays small: the overlay reuses the existing ORB
tests as-is (delegation is invisible) and adds a few targeted
overlay-specific tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eta_engine.strategies.orb_strategy import ORBConfig, ORBStrategy
from eta_engine.strategies.sage_consensus_strategy import SageConsensusConfig

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SageGatedORBConfig:
    """Combined ORB + sage-overlay config."""

    orb: ORBConfig = field(default_factory=ORBConfig)
    sage: SageConsensusConfig = field(default_factory=SageConsensusConfig)
    # Disable the overlay and behave identically to plain ORB. Useful
    # for ablation tests / "what does the gate cost vs. add?" runs.
    overlay_enabled: bool = True


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


def _bar_to_dict(b: BarData) -> dict[str, Any]:
    """Sage bar dict shape (mirrors sage_consensus_strategy._bar_to_dict)."""
    return {
        "ts": b.timestamp.isoformat(),
        "timestamp": b.timestamp,
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
    }


class SageGatedORBStrategy:
    """ORB with a multi-school veto on the breakout direction."""

    def __init__(self, config: SageGatedORBConfig | None = None) -> None:
        self.cfg = config or SageGatedORBConfig()
        # Embed a real ORBStrategy. We DELEGATE the breakout logic
        # entirely; this class is a wrapper that filters its output.
        self._orb = ORBStrategy(self.cfg.orb)

    # -- proxy: ES provider attachment --------------------------------------

    def attach_es_provider(self, provider: Any) -> None:  # noqa: ANN401 - provider is duck-typed
        """Forward to the embedded ORBStrategy."""
        self._orb.attach_es_provider(provider)

    # -- main entry point -----------------------------------------------------

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Run ORB; if it would fire, gate on sage consensus."""
        opened = self._orb.maybe_enter(bar, hist, equity, config)
        if opened is None:
            return None  # ORB itself rejected — nothing to gate

        if not self.cfg.overlay_enabled:
            return opened  # ablation mode: pass through

        # Run sage on the same hist
        from eta_engine.brain.jarvis_v3.sage.base import MarketContext
        from eta_engine.brain.jarvis_v3.sage.consultation import consult_sage

        bars_dicts = [_bar_to_dict(b) for b in hist[-self.cfg.sage.sage_lookback_bars:]]
        if len(bars_dicts) < 25:
            # Not enough bars for the regime detector — let ORB fire as
            # if the overlay were off. Failing-closed here would silence
            # the strategy entirely on the first 24 bars of every run.
            return opened

        ctx = MarketContext(
            bars=bars_dicts,
            side="long" if opened.side == "BUY" else "short",
            entry_price=float(bar.close),
            symbol=bar.symbol,
            instrument_class=self.cfg.sage.instrument_class,
        )
        try:
            report = consult_sage(
                ctx,
                enabled=(
                    set(self.cfg.sage.enabled_schools)
                    if self.cfg.sage.enabled_schools else None
                ),
                parallel=False,
                use_cache=True,
                apply_edge_weights=self.cfg.sage.apply_edge_weights,
            )
        except Exception:  # noqa: BLE001 — sage isolation
            # Sage failure shouldn't kill the strategy; fail-OPEN so
            # the breakout still fires (consistent with ORB behaviour).
            return opened

        # Gate: composite_bias must match the breakout side AND
        # conviction >= threshold AND alignment_score >= threshold.
        # alignment_score uses ctx.side so already side-correct.
        bias_str = report.composite_bias.value
        bias_aligned = (
            (opened.side == "BUY" and bias_str == "long")
            or (opened.side == "SELL" and bias_str == "short")
        )
        if not bias_aligned:
            self._reset_orb_day_state()
            return None
        if report.conviction < self.cfg.sage.min_conviction:
            self._reset_orb_day_state()
            return None
        if report.alignment_score < self.cfg.sage.min_alignment:
            self._reset_orb_day_state()
            return None

        # Trade survived — annotate regime so the audit trail tells us
        # WHY it fired (sage-confirmed vs naked ORB).
        from dataclasses import replace
        return replace(opened, regime="orb_sage_confirmed")

    # -- helpers --------------------------------------------------------------

    def _reset_orb_day_state(self) -> None:
        """Roll back the ORB's ``breakout_taken`` latch + trade count.

        ORB's _Open path increments trades_today and flips
        breakout_taken ON the entry — but we just rejected the entry,
        so those flags must come back off or we lose the rest of the
        day to a single sage veto.
        """
        if self._orb._day is None:
            return
        self._orb._day.breakout_taken = False
        self._orb._day.trades_today = max(0, self._orb._day.trades_today - 1)
