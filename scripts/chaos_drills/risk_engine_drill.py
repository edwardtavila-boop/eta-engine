"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.risk_engine_drill.

Drill: overshoot risk-engine caps; verify the engine rejects.

What this drill asserts
-----------------------
:mod:`core.risk_engine` holds four pure-function guards that the live
session leans on before every size calculation:

* ``dynamic_position_size`` must reject risk_pct > 0.10 (hard cap).
* ``calculate_max_leverage`` must reject an ATR so wide that the
  computed leverage falls under the 5x safety floor.
* ``check_daily_loss_cap`` must flag True when today's PnL breaches
  the per-day equity cap.
* ``check_max_drawdown_kill`` must flag True when drawdown-from-peak
  crosses the kill threshold.

Silent regressions in any of these would quietly raise size, quietly
allow dangerous leverage, or quietly skip a daily-loss halt.  The drill
trips each guard on purpose and verifies the expected behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.core.risk_engine import (
    calculate_max_leverage,
    check_daily_loss_cap,
    check_max_drawdown_kill,
    dynamic_position_size,
)
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_risk_engine"]


def drill_risk_engine(sandbox: Path) -> dict[str, Any]:  # noqa: ARG001 - drill has no disk I/O
    """Trip every core.risk_engine guard and verify rejection / flag."""
    observed: dict[str, Any] = {}

    # Guard 1 -- risk_pct overshoot above the 10% cap must raise.
    try:
        dynamic_position_size(equity=10_000.0, risk_pct=0.25, atr=5.0, price=100.0)
    except ValueError as exc:
        observed["risk_pct_error"] = str(exc)
    else:
        return drill_result(
            "risk_engine",
            passed=False,
            details="dynamic_position_size accepted risk_pct=0.25 (above 10% cap)",
        )

    # Guard 2 -- ATR so wide that max leverage falls below 5x must raise.
    try:
        calculate_max_leverage(price=100.0, atr_14_5m=40.0)
    except ValueError as exc:
        observed["leverage_error"] = str(exc)
    else:
        return drill_result(
            "risk_engine",
            passed=False,
            details="calculate_max_leverage accepted ATR so wide leverage falls below 5x",
        )

    # Guard 3 -- today's losses at 2x the cap must flag the daily halt.
    equity = 10_000.0
    cap_pct = 0.03
    breached = check_daily_loss_cap(
        todays_pnl=-600.0,  # -6% of equity == 2x the 3% cap
        cap_pct=cap_pct,
        equity=equity,
    )
    if not breached:
        return drill_result(
            "risk_engine",
            passed=False,
            details="check_daily_loss_cap did not flag a 2x-cap breach",
        )
    observed["daily_loss_breach"] = True

    # Guard 4 -- 25% drawdown from peak must flag the kill when kill_pct=0.20.
    kill_flag = check_max_drawdown_kill(
        peak_equity=10_000.0,
        current_equity=7_500.0,
        kill_pct=0.20,
    )
    if not kill_flag:
        return drill_result(
            "risk_engine",
            passed=False,
            details="check_max_drawdown_kill did not flag a 25% DD (kill_pct=20%)",
        )
    observed["dd_kill_flag"] = True

    return drill_result(
        "risk_engine",
        passed=True,
        details="all four risk-engine guards rejected or flagged as expected",
        observed=observed,
    )
