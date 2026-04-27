"""LLM cost-budget enforcer (Tier-2 #8 wiring, 2026-04-27).

Wraps ``brain/avengers/cost_forecast.py::CostForecast`` so any
LLM-invoking caller can do::

    from eta_engine.brain.model_policy_budget import allow_llm_call

    if not allow_llm_call(estimated_cost_usd=0.04, tier="opus"):
        # budget exhausted; fall back to a cheaper tier or skip
        return cheaper_path()

The forecaster persists state to ``state/cost_governor/burn.json`` and
has a monthly cap (default $50, configurable via
``ETA_LLM_BUDGET_USD``). When projected burn would breach the cap,
``allow_llm_call`` returns False with a structured reason.

Doesn't replace ``model_policy.py`` -- that one decides WHICH tier to
use; this one decides whether to spend AT ALL given budget pressure.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetVerdict:
    allowed: bool
    reason_code: str
    detail: str
    spent_usd: float = 0.0
    budget_usd: float = 0.0


def _budget_cap_usd() -> float:
    try:
        return float(os.environ.get("ETA_LLM_BUDGET_USD", "50.0"))
    except ValueError:
        return 50.0


def allow_llm_call(*, estimated_cost_usd: float, tier: str = "sonnet") -> BudgetVerdict:
    """Per-call budget gate. Returns BudgetVerdict.

    The default cap is intentionally TIGHT ($50/mo) -- the operator
    can raise via ``ETA_LLM_BUDGET_USD`` env. Critical-tier daemons
    (BATMAN twin-verdict, kaizen close-cycle) should bypass this gate
    by passing tier="critical".
    """
    cap = _budget_cap_usd()

    # Critical-tier always passes (kill-switch logic must not be
    # budget-blocked).
    if tier.lower() == "critical":
        return BudgetVerdict(
            allowed=True, reason_code="critical_bypass",
            detail=f"tier=critical bypasses budget cap ({cap:.2f})",
            budget_usd=cap,
        )

    try:
        from eta_engine.brain.avengers.cost_forecast import CostForecast

        forecaster = CostForecast()
        report = forecaster.report() if hasattr(forecaster, "report") else None
        spent = float(getattr(report, "month_to_date_usd", 0.0)) if report else 0.0

        if spent + estimated_cost_usd > cap:
            return BudgetVerdict(
                allowed=False,
                reason_code="budget_exceeded",
                detail=(
                    f"current spend ${spent:.2f} + estimated ${estimated_cost_usd:.4f} "
                    f"would exceed monthly cap ${cap:.2f}"
                ),
                spent_usd=spent,
                budget_usd=cap,
            )
        return BudgetVerdict(
            allowed=True,
            reason_code="within_budget",
            detail=(
                f"spend ${spent:.2f} + ${estimated_cost_usd:.4f} = ${spent + estimated_cost_usd:.4f} "
                f"<= cap ${cap:.2f}"
            ),
            spent_usd=spent,
            budget_usd=cap,
        )
    except Exception as exc:  # noqa: BLE001
        # If the forecaster is unavailable or errors, FAIL OPEN with a
        # log -- we'd rather make a few extra calls than silently kill
        # JARVIS during an outage of the cost subsystem.
        logger.warning("cost_forecast unavailable: %s -- failing open", exc)
        return BudgetVerdict(
            allowed=True, reason_code="forecast_unavailable",
            detail=f"cost_forecast subsystem error: {exc}",
            budget_usd=cap,
        )
