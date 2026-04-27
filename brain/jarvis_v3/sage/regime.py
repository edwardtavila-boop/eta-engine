"""Regime detector for the sage (Wave-5 #2, 2026-04-27).

Classifies the current market state into one of four regimes:

  * trending  -- high directional momentum + ADX-equivalent
  * ranging   -- price oscillating in a tight band
  * volatile  -- expanded ATR + erratic direction
  * quiet     -- compressed range, low ATR

The confluence aggregator uses the regime to reweight schools:
  * trend_following.WEIGHT *= 1.5 in trending, *= 0.4 in ranging
  * support_resistance.WEIGHT *= 1.4 in ranging, *= 0.6 in trending
  * vpa.WEIGHT *= 1.2 in volatile (volume tells us more when vol is up)
  * order_flow.WEIGHT *= 1.3 in volatile

This module is pure -- it returns a Regime enum + signals dict; the
confluence layer applies the weight modulators.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from eta_engine.brain.jarvis_v3.sage.base import MarketContext


class Regime(StrEnum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"
    QUIET = "quiet"


def _atr_pct(bars: list[dict[str, Any]], period: int = 14) -> float:
    """ATR / close, expressed as percent. 0.0 if insufficient data."""
    if len(bars) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(len(bars) - period, len(bars)):
        h = float(bars[i]["high"])
        l = float(bars[i]["low"])
        prev_c = float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    atr = sum(trs) / len(trs)
    last_close = float(bars[-1]["close"])
    if last_close <= 0:
        return 0.0
    return atr / last_close


def _directional_strength(bars: list[dict[str, Any]], period: int = 20) -> float:
    """|net move| / sum-of-absolute-moves over `period` bars.

    1.0 = perfectly straight line, 0.0 = perfect chop.
    """
    if len(bars) < period + 1:
        return 0.0
    closes = [float(b["close"]) for b in bars[-period - 1:]]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return net / path if path > 0 else 0.0


def detect_regime(ctx: MarketContext) -> tuple[Regime, dict[str, float]]:
    """Classify the market regime + return signals for audit.

    Heuristic:
      * directional_strength >= 0.45 + atr_pct >= median  -> TRENDING
      * directional_strength <  0.20 + atr_pct >= median  -> VOLATILE
      * directional_strength <  0.20 + atr_pct <  median  -> RANGING (chop)
      * atr_pct very low                                  -> QUIET
      * else                                              -> VOLATILE (default)

    Returns (regime, signals_dict).
    """
    if ctx.n_bars < 25:
        return Regime.QUIET, {"reason": "insufficient bars"}

    atr_pct = _atr_pct(ctx.bars, period=14)
    dir_str = _directional_strength(ctx.bars, period=20)

    # Heuristic median ATR thresholds (instrument-agnostic; tune later)
    atr_quiet = 0.001    # 10 bps
    atr_normal = 0.003   # 30 bps

    if atr_pct < atr_quiet:
        regime = Regime.QUIET
    elif dir_str >= 0.45:
        regime = Regime.TRENDING
    elif dir_str < 0.20 and atr_pct >= atr_normal:
        regime = Regime.VOLATILE
    elif dir_str < 0.20:
        regime = Regime.RANGING
    else:
        # Mid-range: bias toward TRENDING if directional, else VOLATILE
        regime = Regime.TRENDING if dir_str >= 0.30 else Regime.VOLATILE

    return regime, {
        "atr_pct": atr_pct,
        "directional_strength": dir_str,
        "atr_quiet_threshold": atr_quiet,
        "atr_normal_threshold": atr_normal,
    }


# Per-school regime modulators applied in the confluence aggregator.
# Schools not listed here use 1.0 (neutral) in every regime.
REGIME_WEIGHT_MULTIPLIERS: dict[str, dict[Regime, float]] = {
    "trend_following": {
        Regime.TRENDING: 1.5,
        Regime.RANGING:  0.4,
        Regime.VOLATILE: 0.7,
        Regime.QUIET:    0.6,
    },
    "support_resistance": {
        Regime.TRENDING: 0.7,
        Regime.RANGING:  1.4,
        Regime.VOLATILE: 1.0,
        Regime.QUIET:    1.0,
    },
    "market_profile": {
        Regime.RANGING:  1.3,  # value area concept shines in rotation
        Regime.TRENDING: 0.85,
        Regime.VOLATILE: 1.0,
        Regime.QUIET:    1.0,
    },
    "vpa": {
        Regime.VOLATILE: 1.2,
        Regime.QUIET:    0.7,
        Regime.TRENDING: 1.0,
        Regime.RANGING:  1.0,
    },
    "order_flow": {
        Regime.VOLATILE: 1.3,
        Regime.QUIET:    0.6,
        Regime.TRENDING: 1.0,
        Regime.RANGING:  1.0,
    },
    "elliott_wave": {
        Regime.TRENDING: 1.2,
        Regime.RANGING:  0.5,
        Regime.VOLATILE: 0.8,
        Regime.QUIET:    0.5,
    },
    "neowave": {
        Regime.TRENDING: 1.3,
        Regime.RANGING:  0.5,
        Regime.VOLATILE: 0.8,
        Regime.QUIET:    0.5,
    },
}


def regime_weight_modulator(school_name: str, regime: Regime | str | None) -> float:
    """Return the multiplier to apply to a school's WEIGHT given regime."""
    if regime is None:
        return 1.0
    if isinstance(regime, str):
        try:
            regime = Regime(regime)
        except ValueError:
            return 1.0
    return REGIME_WEIGHT_MULTIPLIERS.get(school_name, {}).get(regime, 1.0)
