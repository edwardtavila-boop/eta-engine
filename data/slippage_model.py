"""
EVOLUTIONARY TRADING ALGO  //  data.slippage_model
======================================
Tiered slippage model with defaults per symbol, sqrt-qty impact, calibration.
Urgency tiers: PASSIVE / NORMAL / AGGRESSIVE.
"""

from __future__ import annotations

import math
from typing import Any

# Realistic default half-spreads (bps). Cover major crypto + MNQ/NQ ticks.
DEFAULT_SPREAD_BPS: dict[str, float] = {
    "ETHUSDT": 0.5,
    "SOLUSDT": 1.0,
    "XRPUSDT": 2.0,
    "BTCUSDT": 0.3,
    "MNQ": 0.25,  # 0.25 index pts @ ~20500 ≈ 1.2 bps — stored as ticks below
    "NQ": 0.25,
    "ES": 0.25,
    "RTY": 0.1,
    "YM": 1.0,
}

# Rough daily ADV in symbol-native units (used only if caller omits vol_pct).
DEFAULT_DAILY_ADV: dict[str, float] = {
    "ETHUSDT": 500_000.0,
    "SOLUSDT": 1_500_000.0,
    "XRPUSDT": 20_000_000.0,
    "BTCUSDT": 80_000.0,
    "MNQ": 2_000_000.0,
    "NQ": 500_000.0,
    "ES": 1_800_000.0,
}


class SlippageModel:
    """Bps-denominated slippage estimator.

    NORMAL:      0.5 * spread + 0.1 * sqrt(qty/ADV) * vol_pct
    AGGRESSIVE:  1.0 * spread + 0.3 * sqrt(qty/ADV) * vol_pct
    PASSIVE:    -0.5 bps if post-only fill assumed (negative = price improvement)
    """

    def __init__(
        self,
        spread_overrides: dict[str, float] | None = None,
        adv_overrides: dict[str, float] | None = None,
        calibration_factor: float = 1.0,
    ) -> None:
        self.spreads = {**DEFAULT_SPREAD_BPS, **(spread_overrides or {})}
        self.advs = {**DEFAULT_DAILY_ADV, **(adv_overrides or {})}
        self.calibration_factor = calibration_factor

    def estimate(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        spread_ticks: float | None = None,
        vol_pct: float = 1.0,
        urgency: str = "NORMAL",
        daily_adv: float | None = None,
    ) -> float:
        """Return expected slippage in bps (signed).

        Positive for BUY costs / SELL discounts; passive can go negative.
        """
        urg = urgency.upper()
        spread_bps = spread_ticks if spread_ticks is not None else self.spreads.get(symbol, 1.0)
        adv = daily_adv if daily_adv is not None else self.advs.get(symbol, 1e6)
        qty_ratio = max(qty / adv, 1e-9)
        impact = math.sqrt(qty_ratio) * vol_pct

        if urg == "PASSIVE":
            slip = -0.5
        elif urg == "AGGRESSIVE":
            slip = 1.0 * spread_bps + 30.0 * impact
        else:  # NORMAL
            slip = 0.5 * spread_bps + 10.0 * impact

        # side sign: buy pays +, sell pays + (always cost from the trader's PoV)
        _ = side
        return round(self.calibration_factor * slip, 4)

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------
    def calibrate(
        self,
        actual_fills: list[Any],
        theoretical: list[float],
    ) -> dict[str, float]:
        """Fit a single multiplier: factor = mean(actual_bps) / mean(theor_bps).

        actual_fills: objects with .fill_price_bps_vs_mid attribute, or floats.
        """
        actuals = [
            getattr(f, "fill_price_bps_vs_mid", f) if not isinstance(f, (int, float)) else float(f)
            for f in actual_fills
        ]
        if not actuals or not theoretical or len(actuals) != len(theoretical):
            return {"factor": self.calibration_factor, "n": 0}
        mean_a = sum(actuals) / len(actuals)
        mean_t = sum(theoretical) / len(theoretical)
        if abs(mean_t) < 1e-9:
            return {"factor": self.calibration_factor, "n": len(actuals)}
        factor = mean_a / mean_t
        self.calibration_factor = factor
        return {
            "factor": round(factor, 4),
            "n": len(actuals),
            "mean_actual_bps": round(mean_a, 4),
            "mean_theoretical_bps": round(mean_t, 4),
        }
