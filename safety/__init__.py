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
"""

from __future__ import annotations

from eta_engine.safety.live_gate import (
    LiveTradingDisabled,
    assert_live_allowed,
)
from eta_engine.safety.position_cap import (
    PositionCapExceeded,
    assert_within_caps,
)

__all__ = [
    "LiveTradingDisabled",
    "PositionCapExceeded",
    "assert_live_allowed",
    "assert_within_caps",
]
