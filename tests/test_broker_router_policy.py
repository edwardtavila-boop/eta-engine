from __future__ import annotations

from datetime import UTC, datetime

from eta_engine.scripts import broker_router_policy
from eta_engine.scripts.broker_router_pending import PendingOrder


def _order(**overrides: object) -> PendingOrder:
    payload: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "signal_id": "sig-001",
        "side": "BUY",
        "qty": 1.0,
        "symbol": "MNQ",
        "limit_price": 25_000.0,
        "bot_id": "alpha",
        "stop_price": 24_900.0,
        "target_price": 25_100.0,
        "reduce_only": False,
        "execution_lane": "paper",
        "daily_loss_gate_mode": "",
        "daily_loss_gate_active": False,
        "daily_loss_gate_reason": "",
    }
    payload.update(overrides)
    return PendingOrder(**payload)  # type: ignore[arg-type]


def test_router_daily_loss_killswitch_denial_allows_reduce_only_exit() -> None:
    order = _order(reduce_only=True)
    assert (
        broker_router_policy.router_daily_loss_killswitch_denial(
            order,
            is_killswitch_tripped_fn=lambda: (True, "tripped"),
        )
        is None
    )


def test_router_daily_loss_killswitch_denial_skips_advisory_lane() -> None:
    order = _order(daily_loss_gate_mode="advisory")
    assert (
        broker_router_policy.router_daily_loss_killswitch_denial(
            order,
            is_killswitch_tripped_fn=lambda: (True, "tripped"),
        )
        is None
    )


def test_router_daily_loss_killswitch_denial_blocks_when_killswitch_is_tripped() -> None:
    order = _order()
    denial = broker_router_policy.router_daily_loss_killswitch_denial(
        order,
        daily_loss_gate_mode_for_lane_fn=lambda lane: "hard",
        is_killswitch_tripped_fn=lambda: (True, "day_pnl=$-925.50 <= limit=$-300.00"),
    )
    assert denial is not None
    assert denial["gate"] == "daily_loss_killswitch"
    assert "-925.50" in denial["reason"]
    assert denial["context"]["order"]["bot_id"] == "alpha"


def test_router_daily_loss_killswitch_denial_returns_error_payload_when_probe_raises() -> None:
    order = _order()
    denial = broker_router_policy.router_daily_loss_killswitch_denial(
        order,
        daily_loss_gate_mode_for_lane_fn=lambda lane: "hard",
        is_killswitch_tripped_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert denial is not None
    assert denial["gate"] == "daily_loss_killswitch"
    assert "daily_loss_killswitch_error:RuntimeError:boom" in denial["reason"]
