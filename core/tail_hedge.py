"""Tail hedge calculator — P4_SHIELD tail_hedge.

Offline helper that prices two cheap tail-risk hedges against the book:

* **OTM put on SPY** — Black-Scholes payoff for an out-of-the-money put,
  used as disaster insurance against a broad equity crash.
* **Inverse-perp BTC short** — constant-notional short that pays when
  crypto rolls over. Used when the portfolio is crypto-heavy.

Policy
------
The caller supplies live portfolio state (notional, max DD tolerance) and
this module emits:

* recommended hedge size + cost
* expected payoff under a specific loss scenario
* whether to trigger the hedge right now (policy knob ``trigger_dd_pct``)

No live order flow. The allocator consumes the ``TailHedgeDecision`` and
either queues a hedge leg or stores it for manual review.
"""
from __future__ import annotations

import logging
import math
from typing import Literal

from pydantic import BaseModel, Field
from scipy import stats

logger = logging.getLogger(__name__)

HedgeKind = Literal["otm_put_spy", "inverse_perp_btc", "otm_put_btc_deribit"]


class TailHedgePolicy(BaseModel):
    """Configurable trigger thresholds + cost ceilings."""

    trigger_dd_pct: float = 5.0  # portfolio DD beyond this arms hedge
    max_cost_pct_of_equity: float = 0.75  # premium ceiling, % of equity
    target_coverage_pct: float = 60.0  # aim to cover this much of worst-case drain
    default_kind: HedgeKind = "otm_put_spy"


class TailHedgeDecision(BaseModel):
    """Output of :func:`decide` — instruction for the allocator."""

    armed: bool
    kind: HedgeKind
    cost_usd: float
    notional_usd: float
    expected_payoff_usd: float
    coverage_pct: float
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Black-Scholes pricing (for OTM put)
# ---------------------------------------------------------------------------

def _bs_put_price(
    spot: float,
    strike: float,
    t_years: float,
    rate: float,
    sigma: float,
) -> float:
    """Black-Scholes put price. No dividend yield term (approximation)."""
    if t_years <= 0 or sigma <= 0:
        return max(strike - spot, 0.0)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    put_price = strike * math.exp(-rate * t_years) * stats.norm.cdf(-d2) - spot * stats.norm.cdf(-d1)
    return max(float(put_price), 0.0)


# ---------------------------------------------------------------------------
# Price helpers for each kind
# ---------------------------------------------------------------------------

def price_otm_put(
    *,
    spy_spot: float = 500.0,
    otm_pct: float = 10.0,
    days_to_expiry: int = 30,
    implied_vol: float = 0.22,
    risk_free_rate: float = 0.04,
) -> dict[str, float]:
    """Price a single 10%-OTM SPY put. Returns ``{premium_per_share, strike}``."""
    strike = spy_spot * (1.0 - otm_pct / 100.0)
    t_years = days_to_expiry / 365.0
    premium = _bs_put_price(spy_spot, strike, t_years, risk_free_rate, implied_vol)
    return {"premium_per_share": premium, "strike": strike}


def price_otm_put_btc_deribit(
    *,
    btc_spot: float = 60_000.0,
    otm_pct: float = 15.0,
    days_to_expiry: int = 30,
    implied_vol: float = 0.65,
    risk_free_rate: float = 0.04,
) -> dict[str, float]:
    """Price a single 15%-OTM BTC put on Deribit.

    Mirrors :func:`price_otm_put` but with BTC-native IV defaults (Deribit
    ATM IV is typically 50-80%, not 22% like SPY). Premium returned is
    per-BTC (Deribit BTC options are 1 BTC-sized).
    """
    strike = btc_spot * (1.0 - otm_pct / 100.0)
    t_years = days_to_expiry / 365.0
    premium = _bs_put_price(btc_spot, strike, t_years, risk_free_rate, implied_vol)
    return {"premium_per_btc": premium, "strike": strike}


def price_inverse_perp_short(
    *,
    btc_spot: float = 65_000.0,
    funding_pct_per_day: float = -0.01,  # short pays negative funding (earns) or positive (pays)
    days: int = 7,
) -> dict[str, float]:
    """Rough funding-cost estimate for an inverse BTC perp held for ``days``.

    Negative ``funding_pct_per_day`` means shorts receive; positive = pay.
    """
    # Cost as fraction of notional over holding window.
    cost_pct = funding_pct_per_day / 100.0 * days
    return {"cost_pct": cost_pct, "btc_spot": btc_spot}


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------

def decide(
    *,
    equity_usd: float,
    current_dd_pct: float,
    policy: TailHedgePolicy | None = None,
    spy_spot: float = 500.0,
    otm_pct: float = 10.0,
    implied_vol: float = 0.22,
    days_to_expiry: int = 30,
) -> TailHedgeDecision:
    """Decide whether to arm an OTM-put tail hedge right now.

    The policy arms only when:
    * ``current_dd_pct >= policy.trigger_dd_pct`` (portfolio already hurting),
    * and the hedge premium stays below ``policy.max_cost_pct_of_equity``.
    """
    pol = policy or TailHedgePolicy()
    notes: list[str] = []

    quote = price_otm_put(
        spy_spot=spy_spot,
        otm_pct=otm_pct,
        days_to_expiry=days_to_expiry,
        implied_vol=implied_vol,
    )
    premium = quote["premium_per_share"]
    strike = quote["strike"]

    # Target: cover ``target_coverage_pct`` of the worst-case DD projection.
    # We assume worst-case = 2x current DD (crude but defensible).
    projected_loss_usd = equity_usd * (current_dd_pct / 100.0) * 2.0
    target_payoff_usd = projected_loss_usd * (pol.target_coverage_pct / 100.0)
    # Per-share payoff at a -X% move past strike: strike - spot_after = strike - spy*(1-X)
    # Use the OTM percentage itself as the "crash level" → payoff per share = strike - spy*(1-2*otm/100)
    crash_spot = spy_spot * (1.0 - 2.0 * otm_pct / 100.0)
    payoff_per_share = max(strike - crash_spot, 0.0)
    if payoff_per_share <= 0:
        contracts = 0.0
        cost_usd = 0.0
        expected_payoff = 0.0
        notes.append("otm level too shallow to produce positive payoff; recommend wider OTM")
    else:
        # One SPY option contract = 100 shares
        shares_needed = target_payoff_usd / payoff_per_share if payoff_per_share > 0 else 0.0
        contracts = math.ceil(shares_needed / 100.0)
        cost_usd = contracts * 100 * premium
        expected_payoff = contracts * 100 * payoff_per_share

    max_cost_allowed = equity_usd * (pol.max_cost_pct_of_equity / 100.0)
    cost_ok = cost_usd <= max_cost_allowed
    dd_trigger = current_dd_pct >= pol.trigger_dd_pct

    armed = bool(dd_trigger and cost_ok and contracts > 0)
    if not dd_trigger:
        notes.append(f"dd {current_dd_pct:.2f}% below trigger {pol.trigger_dd_pct:.2f}%")
    if not cost_ok:
        notes.append(
            f"hedge cost ${cost_usd:.0f} exceeds ceiling ${max_cost_allowed:.0f}"
        )

    coverage_pct = (
        (expected_payoff / projected_loss_usd * 100.0) if projected_loss_usd > 0 else 0.0
    )

    logger.info(
        "tail_hedge.decide | equity=%.0f dd=%.2f armed=%s contracts=%d cost=%.0f payoff=%.0f",
        equity_usd, current_dd_pct, armed, int(contracts), cost_usd, expected_payoff,
    )

    return TailHedgeDecision(
        armed=armed,
        kind=pol.default_kind,
        cost_usd=round(cost_usd, 2),
        notional_usd=round(contracts * 100 * spy_spot, 2),
        expected_payoff_usd=round(expected_payoff, 2),
        coverage_pct=round(coverage_pct, 2),
        notes=notes,
    )
