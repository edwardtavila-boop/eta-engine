from __future__ import annotations

from eta_engine.scripts import (
    broker_router,
    broker_router_config,
    broker_router_pending,
    broker_router_policy,
    workspace_roots,
)


def test_broker_router_compat_exports_point_at_extracted_owners() -> None:
    assert broker_router.PendingOrder is broker_router_pending.PendingOrder
    assert broker_router.parse_pending_file is broker_router_pending.parse_pending_file
    assert broker_router.pending_order_sanity_denial is broker_router_pending.pending_order_sanity_denial
    assert broker_router.router_daily_loss_killswitch_denial is broker_router_policy.router_daily_loss_killswitch_denial
    assert broker_router._normalize_futures_symbol is broker_router_pending._normalize_futures_symbol
    assert broker_router.normalize_symbol is broker_router_config.normalize_symbol
    assert (
        broker_router.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH
        == workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH
    )


def test_broker_router_all_declares_intended_public_contract() -> None:
    exported = set(broker_router.__all__)
    assert {
        "BrokerRouter",
        "RoutingConfig",
        "PendingOrder",
        "normalize_symbol",
        "parse_pending_file",
        "pending_order_sanity_denial",
        "_normalize_futures_symbol",
        "ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH",
        "router_daily_loss_killswitch_denial",
        "main",
    }.issubset(exported)
    assert "wire_router_bootstrap" not in exported
    assert "wire_router_components" not in exported
