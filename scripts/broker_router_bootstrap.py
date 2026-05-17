from __future__ import annotations

from typing import Any

def wire_router_bootstrap(
    router: Any,
    *,
    failover_cls: Any,
    retry_meta_suffix: str,
    routing_resolver_cls: Any,
    secrets: Any,
    state_io_cls: Any,
    tradovate_venue_cls: Any,
    logger: Any,
) -> None:
    """Attach the constructor-owned state, failover, routing, and path surfaces."""
    router._prop_venue_cache = {}
    router._state_io = state_io_cls(
        state_root=router.state_root,
        retry_meta_suffix=retry_meta_suffix,
        logger=logger,
    )
    router._failover = failover_cls(
        routing_config=router.routing_config,
        smart_router=router.smart_router,
        resolve_venue_adapter=router._resolve_venue_adapter,
        is_transient_failure=router._is_transient_failure,
        logger=logger,
    )
    router._routing = routing_resolver_cls(
        smart_router=router.smart_router,
        prop_venue_cache=router._prop_venue_cache,
        secrets=secrets,
        tradovate_venue_cls=tradovate_venue_cls,
    )
    router.processing_dir = router._state_io.processing_dir
    router.blocked_dir = router._state_io.blocked_dir
    router.archive_dir = router._state_io.archive_dir
    router.quarantine_dir = router._state_io.quarantine_dir
    router.failed_dir = router._state_io.failed_dir
    router.fill_results_dir = router._state_io.fill_results_dir
    router.heartbeat_path = router._state_io.heartbeat_path
    router.gate_pre_trade_path = router._state_io.gate_pre_trade_path
    router.gate_heat_state_path = router._state_io.gate_heat_state_path
    router.gate_journal_path = router._state_io.gate_journal_path
