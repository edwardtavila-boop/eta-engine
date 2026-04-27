"""
EVOLUTIONARY TRADING ALGO  //  risk_engine
==============================
Dynamic position sizing. Pure functions. No state.
Blow up on paper, not on chain.
"""

from __future__ import annotations

from enum import Enum


class RiskTier(Enum):
    """Risk profile tiers. Tighter = smaller position, wider stops."""

    FUTURES = "futures"  # tight: exchange-traded, regulated margin
    SEED = "seed"  # medium: funded account, drawdown rules
    CASINO = "casino"  # high: degen capital, full send


# ---------------------------------------------------------------------------
# Leverage ceiling
# ---------------------------------------------------------------------------


def calculate_max_leverage(
    price: float,
    atr_14_5m: float,
    maint_margin_rate: float = 0.005,
    safety_buffer: float = 0.20,
) -> float:
    """Maximum leverage before liquidation risk becomes unacceptable.

    Formula:
        price / (3.0 * atr * (1 + buffer) + price * mmr)

    Returns the leverage ceiling.  Raises ValueError when the
    result falls below 5x (market too volatile for any position).
    """
    if atr_14_5m <= 0:
        raise ValueError(f"ATR must be positive, got {atr_14_5m}")
    if price <= 0:
        raise ValueError(f"Price must be positive, got {price}")

    denominator = 3.0 * atr_14_5m * (1.0 + safety_buffer) + price * maint_margin_rate
    max_lev = price / denominator

    if max_lev < 5.0:
        raise ValueError(
            f"Max leverage {max_lev:.2f}x below safety floor (5x). ATR={atr_14_5m:.2f} too wide at price={price:.2f}."
        )
    return round(max_lev, 2)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


def dynamic_position_size(
    equity: float,
    risk_pct: float,
    atr: float,
    price: float,
) -> float:
    """USD notional position size based on ATR-scaled risk.

    risk_dollars = equity * risk_pct
    stop_distance = 2 * atr  (standard 2-ATR stop)
    contracts_equiv = risk_dollars / stop_distance
    notional = contracts_equiv * price
    """
    if equity <= 0 or risk_pct <= 0 or atr <= 0 or price <= 0:
        raise ValueError("All inputs must be positive")
    if risk_pct > 0.10:
        raise ValueError(f"Risk percent {risk_pct:.2%} exceeds 10% hard cap")

    risk_dollars = equity * risk_pct
    stop_distance = 2.0 * atr
    contracts = risk_dollars / stop_distance
    return round(contracts * price, 2)


# ---------------------------------------------------------------------------
# Kelly criterion
# ---------------------------------------------------------------------------


def fractional_kelly(
    win_rate: float,
    avg_win_r: float,
    avg_loss_r: float,
    fraction: float = 0.25,
) -> float:
    """Fractional Kelly bet size as fraction of equity.

    kelly = (win_rate / avg_loss_r) - ((1 - win_rate) / avg_win_r)
    Returns kelly * fraction, floored at 0.
    """
    if not 0 < win_rate < 1:
        raise ValueError(f"Win rate must be (0,1), got {win_rate}")
    if avg_win_r <= 0 or avg_loss_r <= 0:
        raise ValueError("Win/loss R values must be positive")
    if not 0 < fraction <= 1:
        raise ValueError(f"Fraction must be (0,1], got {fraction}")

    kelly = (win_rate / avg_loss_r) - ((1.0 - win_rate) / avg_win_r)
    return round(max(kelly * fraction, 0.0), 6)


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------


def check_daily_loss_cap(
    todays_pnl: float,
    cap_pct: float,
    equity: float,
) -> bool:
    """True if today's losses have breached the daily cap. Trading blocked."""
    if equity <= 0:
        raise ValueError("Equity must be positive")
    if cap_pct <= 0:
        raise ValueError("Cap percent must be positive")

    loss_limit = equity * cap_pct
    return todays_pnl <= -loss_limit


def check_max_drawdown_kill(
    peak_equity: float,
    current_equity: float,
    kill_pct: float,
) -> bool:
    """True if drawdown from peak exceeds kill threshold. Halt all trading."""
    if peak_equity <= 0:
        raise ValueError("Peak equity must be positive")
    if not 0 < kill_pct <= 1:
        raise ValueError(f"Kill pct must be (0,1], got {kill_pct}")

    drawdown = (peak_equity - current_equity) / peak_equity
    return drawdown >= kill_pct


# ---------------------------------------------------------------------------
# Liquidation math
# ---------------------------------------------------------------------------


def liquidation_distance(
    entry_price: float,
    leverage: float,
    margin_mode: str = "isolated",
    maint_rate: float = 0.005,
) -> float:
    """Price distance to liquidation from entry.

    Isolated:  distance = entry * (1/leverage - maint_rate)
    Cross:     distance = entry * (1/leverage - maint_rate) * 0.85
               (cross uses portfolio margin; 85% approximation)
    """
    if entry_price <= 0 or leverage <= 0:
        raise ValueError("Entry price and leverage must be positive")
    if margin_mode not in ("isolated", "cross"):
        raise ValueError(f"Unknown margin mode: {margin_mode}")

    base = entry_price * (1.0 / leverage - maint_rate)
    if base <= 0:
        raise ValueError(
            f"Leverage {leverage}x too high for maint_rate {maint_rate}. Liquidation distance is negative."
        )

    if margin_mode == "cross":
        base *= 0.85

    return round(base, 2)
