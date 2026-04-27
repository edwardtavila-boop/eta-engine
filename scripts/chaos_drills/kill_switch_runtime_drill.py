"""EVOLUTIONARY TRADING ALGO // scripts.chaos_drills.kill_switch_runtime_drill.

Drill: trip the runtime kill-switch; verify the verdict action == FLATTEN_ALL.

What this drill asserts
-----------------------
:mod:`core.kill_switch_runtime` is pure policy. The drill constructs the
worst-case portfolio snapshot (daily loss above the configured cap),
calls ``evaluate(...)``, and confirms the returned ``KillVerdict``:

* uses ``KillAction.FLATTEN_ALL``
* carries ``KillSeverity.CRITICAL``
* is scoped to ``global``
* includes a non-empty ``reason`` string so the operator knows why

A silent regression in the global-trip path would show up as either a
different action (HALVE_SIZE) or no verdict at all.

Sandbox: no disk artefacts beyond the config file we materialize.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from eta_engine.core.kill_switch_runtime import (
    KillAction,
    KillSeverity,
    KillSwitch,
    PortfolioSnapshot,
)
from eta_engine.scripts.chaos_drills._common import drill_result

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drill_kill_switch_runtime"]


_DRILL_CFG: dict[str, Any] = {
    "global": {
        "max_drawdown_kill_pct_of_portfolio": 10.0,
        "daily_loss_cap_pct_of_portfolio": 3.0,
    },
    "tier_a": {},
    "tier_b": {},
}


def drill_kill_switch_runtime(sandbox: Path) -> dict[str, Any]:
    """Force a global flatten and verify the verdict."""
    cfg_path = sandbox / "kill_switch.yaml"
    cfg_path.write_text(yaml.safe_dump(_DRILL_CFG), encoding="utf-8")
    ks = KillSwitch.from_yaml(cfg_path)

    # Portfolio sitting at a 5% daily loss -- well past the 3% cap.
    portfolio = PortfolioSnapshot(
        total_equity_usd=95_000.0,
        peak_equity_usd=100_000.0,
        daily_realized_pnl_usd=-5_000.0,
    )
    verdicts = ks.evaluate(bots=[], portfolio=portfolio)
    if not verdicts:
        return drill_result(
            "kill_switch_runtime",
            passed=False,
            details="KillSwitch.evaluate returned an empty verdict list",
        )
    first = verdicts[0]
    if first.action is not KillAction.FLATTEN_ALL:
        return drill_result(
            "kill_switch_runtime",
            passed=False,
            details=(f"daily-loss breach did not produce FLATTEN_ALL: got action={first.action.value}"),
            observed={"action": first.action.value, "scope": first.scope},
        )
    if first.severity is not KillSeverity.CRITICAL:
        return drill_result(
            "kill_switch_runtime",
            passed=False,
            details=(f"FLATTEN_ALL verdict was not CRITICAL severity: got severity={first.severity.value}"),
        )
    if first.scope != "global":
        return drill_result(
            "kill_switch_runtime",
            passed=False,
            details=f"FLATTEN_ALL scope was not 'global': got {first.scope!r}",
        )
    if not first.reason:
        return drill_result(
            "kill_switch_runtime",
            passed=False,
            details="FLATTEN_ALL verdict carried an empty reason string",
        )
    return drill_result(
        "kill_switch_runtime",
        passed=True,
        details="daily-loss breach produced FLATTEN_ALL CRITICAL verdict",
        observed={
            "action": first.action.value,
            "severity": first.severity.value,
            "scope": first.scope,
            "reason": first.reason,
            "evidence": first.evidence,
        },
    )
