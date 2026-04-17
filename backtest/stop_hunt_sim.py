"""Ghost-trader stop-hunt adversarial simulator — P3_PROOF adversarial.

Purpose
-------
An adversarial probe that asks: "if a reasonable actor intentionally ran my
stops at a bad time, how much would it cost me?"

This is the risk-advocate's favorite hammer. It doesn't replace realistic
fill modelling — it complements it. Realistic fills say "typical execution
cost". This module says "worst-credible execution cost under targeted
adversarial pressure".

The simulator takes:

* the set of live positions, each with entry + stop + size
* the raw bar sequence that drove the backtest
* a hunt policy (how many times to trigger, how deep past the stop, what bar
  selection rule, spread assumption)

It returns a :class:`StopHuntReport` with per-position PnL delta, worst hit,
total adversarial PnL, and a strategy-level robustness score. Downstream
risk code can use this to decide whether a strategy's edge survives a
hostile market.

No SDK coupling, no live order flow. Pure offline analysis.
"""
from __future__ import annotations

import logging
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Side = Literal["long", "short"]
HuntMode = Literal["nearest_high", "nearest_low", "random", "worst_bar"]


class Position(BaseModel):
    """Single open position snapshot."""

    symbol: str
    side: Side
    entry_price: float
    stop_price: float
    size_contracts: float
    point_value_usd: float = 1.0  # dollars per 1.0 price move per contract


class StopHuntPolicy(BaseModel):
    """Configurable adversarial parameters."""

    penetration_ticks: float = 1.0  # how far past the stop the hunt pushes
    tick_size: float = 0.25
    spread_ticks: float = 1.0  # extra slippage on exit fill
    hunts_per_position: int = 1  # number of hunt attempts per position
    mode: HuntMode = "worst_bar"


class PositionResult(BaseModel):
    """Per-position hunt outcome."""

    symbol: str
    side: Side
    entry_price: float
    stop_price: float
    hunt_fill_price: float
    slippage_ticks: float
    pnl_usd: float
    hunted: bool


class StopHuntReport(BaseModel):
    """Aggregate report across all positions."""

    policy: StopHuntPolicy
    positions: list[PositionResult] = Field(default_factory=list)
    total_pnl_usd: float = 0.0
    worst_single_pnl_usd: float = 0.0
    worst_symbol: str | None = None
    hunt_hit_rate: float = 0.0  # fraction of positions that would've been stopped
    robustness_score: float = 0.0  # 1.0 = unharmed, 0.0 = fully drained
    notes: list[str] = Field(default_factory=list)


def _bar_reaches_stop(
    bars_high: np.ndarray,
    bars_low: np.ndarray,
    stop_price: float,
    side: Side,
    penetration_price: float,
) -> bool:
    """Return True if any bar in the series penetrates the stop + overshoot."""
    if side == "long":
        # Long stops trigger on a downward spike; bar low must undercut stop.
        trigger_price = stop_price - penetration_price
        return bool(np.any(bars_low <= trigger_price))
    # Short stops trigger on upward spike; bar high must exceed stop.
    trigger_price = stop_price + penetration_price
    return bool(np.any(bars_high >= trigger_price))


def _fill_price(stop_price: float, side: Side, policy: StopHuntPolicy) -> float:
    """Compute the fill price after penetration + spread slippage."""
    overshoot = policy.penetration_ticks * policy.tick_size
    spread = policy.spread_ticks * policy.tick_size
    if side == "long":
        # Long stop-out: price drops below stop, we fill at stop - overshoot - spread
        return stop_price - overshoot - spread
    # Short stop-out: price rises above stop, we fill at stop + overshoot + spread
    return stop_price + overshoot + spread


def _pnl_usd(position: Position, fill_price: float) -> float:
    """Realized PnL if position hit a stop-out at ``fill_price``."""
    delta = (
        fill_price - position.entry_price
        if position.side == "long"
        else position.entry_price - fill_price
    )
    return float(delta * position.size_contracts * position.point_value_usd)


def simulate(
    positions: list[Position],
    bars_high: np.ndarray,
    bars_low: np.ndarray,
    *,
    policy: StopHuntPolicy | None = None,
) -> StopHuntReport:
    """Run the stop-hunt simulator across a set of positions.

    Parameters
    ----------
    positions
        List of open positions to probe.
    bars_high, bars_low
        1D arrays of per-bar high/low prices. The simulator tests whether
        any bar penetrates the stop + overshoot.
    policy
        Hunt aggressiveness. Defaults to a conservative ``StopHuntPolicy()``.
    """
    pol = policy or StopHuntPolicy()
    if bars_high.shape != bars_low.shape:
        raise ValueError(f"bars_high/bars_low shape mismatch: {bars_high.shape} vs {bars_low.shape}")
    if bars_high.ndim != 1:
        raise ValueError(f"bars must be 1D, got {bars_high.shape}")

    results: list[PositionResult] = []
    notes: list[str] = []

    for pos in positions:
        penetration_price = pol.penetration_ticks * pol.tick_size
        hunted = _bar_reaches_stop(bars_high, bars_low, pos.stop_price, pos.side, penetration_price)
        if hunted:
            fill_price = _fill_price(pos.stop_price, pos.side, pol)
            slip_ticks = pol.penetration_ticks + pol.spread_ticks
            pnl = _pnl_usd(pos, fill_price)
        else:
            fill_price = pos.entry_price  # untouched
            slip_ticks = 0.0
            pnl = 0.0

        results.append(PositionResult(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            stop_price=pos.stop_price,
            hunt_fill_price=round(fill_price, 4),
            slippage_ticks=slip_ticks,
            pnl_usd=round(pnl, 2),
            hunted=hunted,
        ))

    total_pnl = float(sum(r.pnl_usd for r in results))
    hit_rate = float(sum(1 for r in results if r.hunted) / max(len(results), 1))

    if results:
        worst = min(results, key=lambda r: r.pnl_usd)
        worst_pnl = worst.pnl_usd
        worst_sym: str | None = worst.symbol if worst_pnl < 0 else None
    else:
        worst_pnl = 0.0
        worst_sym = None

    # Robustness score: fraction of equity left after adversarial drain, floored at 0.
    # We approximate "equity at risk" as sum of abs(entry - stop) * size * point_value.
    total_risk = float(sum(
        abs(p.entry_price - p.stop_price) * p.size_contracts * p.point_value_usd
        for p in positions
    ))
    if total_risk > 0:
        drain_ratio = min(abs(total_pnl) / total_risk, 1.0) if total_pnl < 0 else 0.0
        robustness = max(0.0, 1.0 - drain_ratio)
    else:
        robustness = 1.0

    if hit_rate > 0.5:
        notes.append(f"hunt_hit_rate>{hit_rate:.2f} — stops clustered in hostile range")
    if robustness < 0.5:
        notes.append(f"robustness<{robustness:.2f} — adversarial drain exceeds 50% of risked equity")

    logger.info(
        "stop_hunt_sim | positions=%d hit_rate=%.2f total_pnl=%.2f robustness=%.3f",
        len(positions), hit_rate, total_pnl, robustness,
    )

    return StopHuntReport(
        policy=pol,
        positions=results,
        total_pnl_usd=round(total_pnl, 2),
        worst_single_pnl_usd=round(worst_pnl, 2),
        worst_symbol=worst_sym,
        hunt_hit_rate=round(hit_rate, 4),
        robustness_score=round(robustness, 4),
        notes=notes,
    )
