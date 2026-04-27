"""EVOLUTIONARY TRADING ALGO // safety.

Pre-route safety gates that fail closed when an operator-controlled
guard is engaged. Each gate is permissive by default (so paper /
test environments don't need explicit opt-in) but raises a typed
exception the moment its kill signal is set.

Exposed gates:

* :func:`live_gate.assert_live_allowed` -- raises
  :class:`live_gate.LiveTradingDisabled` when the firm is halted or
  live trading is explicitly disabled.
* :func:`position_cap.assert_within_caps` -- raises
  :class:`position_cap.PositionCapExceeded` when an order would push
  the running position beyond the configured per-(side, venue, symbol)
  contract limit.
* :func:`fleet_risk_gate.assert_fleet_within_budget` -- raises
  :class:`fleet_risk_gate.FleetRiskBreach` when the fleet's same-day
  aggregate P&L drops below the daily-loss budget. No-op when no
  gate has been registered (paper / test paths).
"""

from __future__ import annotations

from eta_engine.safety.fleet_risk_gate import (
    FleetRiskBreach,
    FleetRiskGate,
    assert_fleet_within_budget,
    get_fleet_risk_gate,
    register_fleet_risk_gate,
)
from eta_engine.safety.live_gate import (
    LiveTradingDisabled,
    assert_live_allowed,
)
from eta_engine.safety.position_cap import (
    PositionCapExceeded,
    assert_within_caps,
)

__all__ = [
    "FleetRiskBreach",
    "FleetRiskGate",
    "LiveTradingDisabled",
    "PositionCapExceeded",
    "assert_fleet_within_budget",
    "assert_live_allowed",
    "assert_within_caps",
    "get_fleet_risk_gate",
    "register_fleet_risk_gate",
]
