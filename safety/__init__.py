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
* :func:`cross_bot_position_tracker.assert_fleet_position_cap` --
  raises :class:`cross_bot_position_tracker.FleetPositionCapExceeded`
  when an order would push the fleet net position for a symbol root
  beyond its configured cap. No-op when no tracker has been
  registered. Composes with the per-order ``position_cap`` gate to
  prevent two bots routed to the same root from accidentally
  combining into a fleet-level position the operator did not
  authorise.
"""

from __future__ import annotations

from eta_engine.safety.cross_bot_position_tracker import (
    CrossBotPositionTracker,
    FleetPositionCapExceeded,
    PropSleeveCapExceeded,
    assert_fleet_position_cap,
    assert_prop_sleeve_cap,
    get_cross_bot_position_tracker,
    register_cross_bot_position_tracker,
)
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
    "CrossBotPositionTracker",
    "FleetPositionCapExceeded",
    "FleetRiskBreach",
    "FleetRiskGate",
    "LiveTradingDisabled",
    "PositionCapExceeded",
    "PropSleeveCapExceeded",
    "assert_fleet_position_cap",
    "assert_prop_sleeve_cap",
    "assert_fleet_within_budget",
    "assert_live_allowed",
    "assert_within_caps",
    "get_cross_bot_position_tracker",
    "get_fleet_risk_gate",
    "register_cross_bot_position_tracker",
    "register_fleet_risk_gate",
]
