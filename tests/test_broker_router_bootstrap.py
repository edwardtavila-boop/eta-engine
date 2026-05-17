from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from eta_engine.scripts.broker_router_bootstrap import wire_router_bootstrap
from eta_engine.scripts.broker_router_failover import BrokerRouterFailover
from eta_engine.scripts.broker_router_routing import BrokerRouterRoutingResolver
from eta_engine.scripts.broker_router_state import BrokerRouterStateIO


class _NoopSmartRouter:
    pass


def test_wire_router_bootstrap_assigns_state_routing_and_paths(tmp_path: Path) -> None:
    router = SimpleNamespace(
        state_root=tmp_path / "state",
        routing_config=object(),
        smart_router=_NoopSmartRouter(),
        _resolve_venue_adapter=lambda *args, **kwargs: None,
        _is_transient_failure=lambda *args, **kwargs: False,
    )

    wire_router_bootstrap(
        router,
        retry_meta_suffix=".retry_meta.json",
        logger=logging.getLogger("test_broker_router_bootstrap"),
    )

    assert isinstance(router._state_io, BrokerRouterStateIO)
    assert isinstance(router._failover, BrokerRouterFailover)
    assert isinstance(router._routing, BrokerRouterRoutingResolver)
    assert router._prop_venue_cache == {}
    assert router.processing_dir == router._state_io.processing_dir
    assert router.blocked_dir == router._state_io.blocked_dir
    assert router.archive_dir == router._state_io.archive_dir
    assert router.quarantine_dir == router._state_io.quarantine_dir
    assert router.failed_dir == router._state_io.failed_dir
    assert router.fill_results_dir == router._state_io.fill_results_dir
    assert router.heartbeat_path == router._state_io.heartbeat_path
    assert router.gate_pre_trade_path == router._state_io.gate_pre_trade_path
    assert router.gate_heat_state_path == router._state_io.gate_heat_state_path
    assert router.gate_journal_path == router._state_io.gate_journal_path
