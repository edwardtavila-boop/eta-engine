"""Pair arbitrage scanner (Wave-15, 2026-04-27).

Spot opportunities where two correlated instruments (MNQ vs ES1,
BTC futures vs spot ETF, etc.) drift far enough apart that the
spread mean-reverts profitably.

This is the math layer ONLY -- no data fetching here. Caller
supplies the price series for each leg; the scanner returns
ArbitrageSignal objects ranked by z-score of current basis.

Method:
  * For each pair, compute the rolling-window basis (price_a - hedge_ratio * price_b)
  * Estimate moments of the basis (mean, std)
  * Current basis -> z-score
  * |z| >= entry_threshold -> emit signal with direction (long-short / short-long)
  * Hedge ratio default = OLS slope of price_a on price_b

Pure stdlib + math.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PairSpec:
    """Definition of one pair to scan."""

    label: str  # e.g. "MNQ_vs_ES1"
    leg_a: str  # e.g. "MNQ"
    leg_b: str  # e.g. "ES1"
    prices_a: list[float]  # historical prices, oldest -> newest
    prices_b: list[float]  # same length
    lookback_bars: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.5


@dataclass
class ArbitrageSignal:
    """One detected pair-trade opportunity."""

    label: str
    leg_a: str
    leg_b: str
    hedge_ratio: float
    current_basis: float
    basis_mean: float
    basis_std: float
    z_score: float
    direction: str  # "long_a_short_b" / "short_a_long_b"
    target_z: float  # exit at |z| <= target
    note: str = ""


@dataclass
class ScanReport:
    n_pairs: int
    n_signals: int
    signals: list[ArbitrageSignal] = field(default_factory=list)


def _ols_slope(xs: list[float], ys: list[float]) -> float:
    """OLS slope of ys on xs."""
    n = min(len(xs), len(ys))
    if n < 3:
        return 1.0
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    return num / den if den > 0 else 1.0


def _moments(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var)


def scan_pair(spec: PairSpec) -> ArbitrageSignal | None:
    """Score one pair. Returns a signal iff |z| >= entry_z."""
    n = min(len(spec.prices_a), len(spec.prices_b))
    if n < spec.lookback_bars:
        return None
    a = spec.prices_a[-spec.lookback_bars :]
    b = spec.prices_b[-spec.lookback_bars :]

    # Hedge ratio = OLS slope of a on b
    hedge_ratio = _ols_slope(b, a)

    # Historical basis (OOS-safe: use bars [0..n-2] for stats, last for entry)
    historical_basis = [a[i] - hedge_ratio * b[i] for i in range(len(a) - 1)]
    mean, std = _moments(historical_basis)
    if std == 0:
        return None
    current_basis = a[-1] - hedge_ratio * b[-1]
    z = (current_basis - mean) / std

    if abs(z) < spec.entry_z:
        return None

    # Direction: if basis ABOVE mean (z > 0), basis will revert DOWN
    #   -> short A, long B (close)
    # If z < 0, basis will revert UP -> long A, short B
    direction = "short_a_long_b" if z > 0 else "long_a_short_b"
    return ArbitrageSignal(
        label=spec.label,
        leg_a=spec.leg_a,
        leg_b=spec.leg_b,
        hedge_ratio=round(hedge_ratio, 4),
        current_basis=round(current_basis, 4),
        basis_mean=round(mean, 4),
        basis_std=round(std, 4),
        z_score=round(z, 3),
        direction=direction,
        target_z=spec.exit_z,
        note=(f"basis {current_basis:+.4f} vs mean {mean:+.4f} (z={z:+.2f}); revert to |z|<={spec.exit_z}"),
    )


def scan_pairs(specs: list[PairSpec]) -> ScanReport:
    """Run scan_pair on every spec; return ranked signals."""
    signals: list[ArbitrageSignal] = []
    for spec in specs:
        try:
            sig = scan_pair(spec)
            if sig is not None:
                signals.append(sig)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pair_arb_scanner: scan_pair failed for %s (%s)",
                spec.label,
                exc,
            )
    signals.sort(key=lambda s: abs(s.z_score), reverse=True)
    return ScanReport(
        n_pairs=len(specs),
        n_signals=len(signals),
        signals=signals,
    )
