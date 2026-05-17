from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eta_engine.core.execution_lanes import (
    daily_loss_gate_mode_for_lane,
    gate_advisory,
    gate_inactive,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from eta_engine.scripts.broker_router_pending import PendingOrder


def router_daily_loss_killswitch_denial(
    order: PendingOrder,
    *,
    daily_loss_gate_mode_for_lane_fn: Callable[[str], str] = daily_loss_gate_mode_for_lane,
    gate_advisory_fn: Callable[[str], bool] = gate_advisory,
    gate_inactive_fn: Callable[[str], bool] = gate_inactive,
    is_killswitch_tripped_fn: Callable[[], tuple[bool, str]] | None = None,
) -> dict[str, Any] | None:
    """Return a router-side daily-loss denial for new entries."""
    if order.reduce_only:
        return None
    gate_mode = str(order.daily_loss_gate_mode or "").strip().lower()
    if not gate_mode:
        gate_mode = daily_loss_gate_mode_for_lane_fn(order.execution_lane)
    if gate_advisory_fn(gate_mode) or gate_inactive_fn(gate_mode):
        return None
    if is_killswitch_tripped_fn is None:
        try:
            from eta_engine.scripts.daily_loss_killswitch import (  # noqa: PLC0415
                is_killswitch_tripped,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "gate": "daily_loss_killswitch",
                "allow": False,
                "reason": f"daily_loss_killswitch_unavailable:{type(exc).__name__}:{exc}",
                "context": {"order": order.to_dict()},
            }
        is_killswitch_tripped_fn = is_killswitch_tripped
    try:
        tripped, reason = is_killswitch_tripped_fn()
    except Exception as exc:  # noqa: BLE001
        return {
            "gate": "daily_loss_killswitch",
            "allow": False,
            "reason": f"daily_loss_killswitch_error:{type(exc).__name__}:{exc}",
            "context": {"order": order.to_dict()},
        }
    if not tripped:
        return None
    return {
        "gate": "daily_loss_killswitch",
        "allow": False,
        "reason": str(reason),
        "context": {"order": order.to_dict()},
    }
