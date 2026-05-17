from __future__ import annotations

from pathlib import Path

from eta_engine.scripts import broker_router
from eta_engine.scripts.broker_router_components import wire_router_components
from eta_engine.scripts.broker_router_errors import BrokerRouterErrorHandlers
from eta_engine.scripts.broker_router_gates import BrokerRouterGateEvaluator
from eta_engine.scripts.broker_router_lifecycle import BrokerRouterLifecycleDriver
from eta_engine.scripts.broker_router_ops import BrokerRouterOpsSurface
from eta_engine.scripts.broker_router_polling import BrokerRouterPolling
from eta_engine.scripts.broker_router_reporting import BrokerRouterReporting
from eta_engine.scripts.broker_router_resolution import BrokerRouterResolution
from eta_engine.scripts.broker_router_runtime import BrokerRouterRuntimeControl
from eta_engine.scripts.broker_router_screening import BrokerRouterScreening
from eta_engine.scripts.broker_router_submission import BrokerRouterSubmission


class _NoopSmartRouter:
    def choose_venue(self, symbol: str, qty: float, urgency: str = "normal") -> None:
        return None


class _NoopJournal:
    def append(self, event: object) -> object:
        return event


def test_wire_router_components_assigns_expected_helper_surfaces(tmp_path: Path) -> None:
    router = broker_router.BrokerRouter(
        pending_dir=tmp_path / "pending",
        state_root=tmp_path / "state",
        smart_router=_NoopSmartRouter(),
        journal=_NoopJournal(),
    )

    wire_router_components(
        router,
        asset_class_for_symbol=broker_router._asset_class_for_symbol,
        backoff_cap_s=broker_router.BACKOFF_CAP_S,
        daily_loss_killswitch_denial=broker_router.router_daily_loss_killswitch_denial,
        env_float=broker_router._env_float,
        env_int=broker_router._env_int,
        extract_broker_fill_ts=broker_router._extract_broker_fill_ts,
        gate_bootstrap_enabled=broker_router._gate_bootstrap_enabled,
        live_money_env=broker_router._LIVE_MONEY_ENV,
        load_build_default_chain=lambda: broker_router._load_build_default_chain(),
        logger=broker_router.logger,
        parse_pending_file=broker_router.parse_pending_file,
        pending_order_sanity_denial=broker_router.pending_order_sanity_denial,
        readiness_enforced=broker_router._readiness_enforced,
        readiness_snapshot_path=lambda: broker_router.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH,
        retry_meta_suffix=broker_router.RETRY_META_SUFFIX,
    )

    assert isinstance(router._errors, BrokerRouterErrorHandlers)
    assert isinstance(router._ops, BrokerRouterOpsSurface)
    assert isinstance(router._reporting, BrokerRouterReporting)
    assert isinstance(router._gates, BrokerRouterGateEvaluator)
    assert isinstance(router._submission, BrokerRouterSubmission)
    assert isinstance(router._screening, BrokerRouterScreening)
    assert isinstance(router._resolution, BrokerRouterResolution)
    assert isinstance(router._polling, BrokerRouterPolling)
    assert isinstance(router._runtime, BrokerRouterRuntimeControl)
    assert isinstance(router._lifecycle, BrokerRouterLifecycleDriver)
