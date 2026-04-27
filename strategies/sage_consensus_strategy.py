"""
EVOLUTIONARY TRADING ALGO  //  strategies.sage_consensus_strategy
==================================================================
Sage-consensus entry strategy — fires on a high-conviction
multi-school directional vote from the JARVIS sage.

Why this strategy
-----------------
``brain/jarvis_v3/sage`` ships a 22-school consultation layer that
aggregates classical (Dow, Wyckoff, Elliott, Fibonacci, Gann),
modern (SMC/ICT, order flow, neowave, Weis/Wyckoff), and modern-
quantitative (seasonality, vol regime, statistical significance,
red team, options greeks, cross-asset correlation, ML, on-chain,
funding) market-theory schools into a single weighted directional
verdict per bar.

Existing strategies (ORB / DRB / crypto family) hard-code one
specific edge each. Sage's strength is the OPPOSITE: an ensemble
read across many independent edges where each school's vote
carries a learned weight. When the ensemble lines up — say, dow
+ wyckoff + smc_ict + trend_following all bullish with conviction
> 0.7 — the trade is meaningfully different from any single-edge
strategy.

This strategy turns that consensus into a tradable signal:

  * Every bar, build a sage MarketContext from the recent N bars.
  * Run consult_sage(); examine composite_bias + conviction +
    consensus_pct.
  * Fire BUY when composite_bias == LONG AND conviction >=
    min_conviction AND consensus_pct >= min_consensus AND
    alignment_score >= min_alignment.
  * Fire SELL on the symmetric SHORT condition.
  * Use ATR for stop/target sizing — same exit machinery as ORB.

Limitations
-----------
* Sage is computationally heavy (22 schools per bar). The
  consultation cache helps, but on fine timeframes (1m/5m) this
  is still ~10-50ms per bar. Don't run on tick data.
* Sage's edge tracker learns from labeled outcomes; in a fresh
  backtest the learned weights start at 1.0. The strategy still
  works (uses base weights), but learned modulation only kicks in
  during paper / live runs that label trades after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eta_engine.backtest.engine import _Open
    from eta_engine.backtest.models import BacktestConfig
    from eta_engine.core.data_pipeline import BarData


@dataclass(frozen=True)
class SageConsensusConfig:
    """Knobs for the sage-consensus strategy."""

    # Sage gates
    min_conviction: float = 0.55
    min_consensus: float = 0.55
    min_alignment: float = 0.65
    sage_lookback_bars: int = 200
    enabled_schools: frozenset[str] = frozenset()
    apply_edge_weights: bool = False

    # Risk / exits
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    rr_target: float = 2.0
    risk_per_trade_pct: float = 0.01

    # Hygiene
    min_bars_between_trades: int = 6
    max_trades_per_day: int = 3
    warmup_bars: int = 60
    instrument_class: str | None = None


def _bar_to_dict(b: BarData) -> dict[str, Any]:
    """Convert engine BarData to the dict shape sage schools expect."""
    return {
        "ts": b.timestamp.isoformat(),
        "timestamp": b.timestamp,
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume),
    }


class SageConsensusStrategy:
    """Multi-school weighted-vote entry for any liquid instrument."""

    def __init__(self, config: SageConsensusConfig | None = None) -> None:
        self.cfg = config or SageConsensusConfig()
        self._bars_seen: int = 0
        self._last_entry_idx: int | None = None
        self._trades_today: int = 0
        self._last_day: object | None = None

    def maybe_enter(
        self,
        bar: BarData,
        hist: list[BarData],
        equity: float,
        config: BacktestConfig,
    ) -> _Open | None:
        """Return an _Open or None. Same engine contract as ORB."""
        bar_date = bar.timestamp.date()
        if self._last_day != bar_date:
            self._last_day = bar_date
            self._trades_today = 0
        self._bars_seen += 1

        if self._bars_seen < self.cfg.warmup_bars:
            return None
        if len(hist) < self.cfg.atr_period + 1:
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if (
            self._last_entry_idx is not None
            and (self._bars_seen - self._last_entry_idx)
            < self.cfg.min_bars_between_trades
        ):
            return None

        # Lazy import — sage pulls in 22 schools and is heavy.
        from eta_engine.brain.jarvis_v3.sage.base import MarketContext
        from eta_engine.brain.jarvis_v3.sage.consultation import consult_sage

        bars_dicts = [_bar_to_dict(b) for b in hist[-self.cfg.sage_lookback_bars:]]
        if len(bars_dicts) < 25:  # regime detector minimum
            return None

        ctx = MarketContext(
            bars=bars_dicts,
            side="long",
            entry_price=float(bar.close),
            symbol=bar.symbol,
            instrument_class=self.cfg.instrument_class,
        )
        try:
            report = consult_sage(
                ctx,
                enabled=set(self.cfg.enabled_schools) if self.cfg.enabled_schools else None,
                parallel=False,
                use_cache=True,
                apply_edge_weights=self.cfg.apply_edge_weights,
            )
        except Exception:  # noqa: BLE001
            return None

        bias = report.composite_bias.value
        if bias not in ("long", "short"):
            return None
        side = "BUY" if bias == "long" else "SELL"

        if report.conviction < self.cfg.min_conviction:
            return None
        if report.consensus_pct < self.cfg.min_consensus:
            return None

        # alignment_score uses ctx.side="long". Translate to real
        # alignment for the chosen side.
        real_alignment = (
            report.alignment_score if side == "BUY"
            else 1.0 - report.alignment_score
        )
        if real_alignment < self.cfg.min_alignment:
            return None

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
        qty = risk_usd / stop_dist
        if qty <= 0.0:
            return None

        entry_price = bar.close
        if side == "BUY":
            stop = entry_price - stop_dist
            target = entry_price + self.cfg.rr_target * stop_dist
        else:
            stop = entry_price + stop_dist
            target = entry_price - self.cfg.rr_target * stop_dist

        from eta_engine.backtest.engine import _Open

        opened = _Open(
            entry_bar=bar, side=side, qty=qty, entry_price=entry_price,
            stop=stop, target=target, risk_usd=risk_usd,
            confluence=10.0 * report.conviction,
            leverage=1.0,
            regime=f"sage_{bias}",
        )
        self._last_entry_idx = self._bars_seen
        self._trades_today += 1
        return opened
