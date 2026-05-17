from __future__ import annotations

from eta_engine.scripts.broker_router_errors import BrokerRouterErrorHandlers
from eta_engine.scripts.broker_router_gates import BrokerRouterGateEvaluator
from eta_engine.scripts.broker_router_lifecycle import BrokerRouterLifecycleDriver
from eta_engine.scripts.broker_router_ops import BrokerRouterOpsSurface
from eta_engine.scripts.broker_router_polling import BrokerRouterPolling
from eta_engine.scripts.broker_router_reporting import BrokerRouterReporting
from eta_engine.scripts.broker_router_resolution import BrokerRouterResolution
from eta_engine.scripts.broker_router_runtime import BrokerRouterRuntimeControl
from eta_engine.scripts.broker_router_screening import BrokerRouterScreening
from eta_engine.scripts.broker_router_state import EMPTY_RETRY_META
from eta_engine.scripts.broker_router_submission import BrokerRouterSubmission
from eta_engine.scripts.prop_risk_governor import prop_order_risk_denial
from eta_engine.venues.router import DORMANT_BROKERS


def wire_router_components(
    router: object,
    *,
    asset_class_for_symbol: object,
    backoff_cap_s: float,
    daily_loss_killswitch_denial: object,
    env_float: object,
    env_int: object,
    extract_broker_fill_ts: object,
    gate_bootstrap_enabled: object,
    live_money_env: str,
    load_build_default_chain: object,
    logger: object,
    parse_pending_file: object,
    pending_order_sanity_denial: object,
    readiness_enforced: object,
    readiness_snapshot_path: object,
    retry_meta_suffix: str,
) -> None:
    """Attach the extracted runtime/helper surfaces to a router instance."""
    router._errors = BrokerRouterErrorHandlers(
        counts=router._counts,
        dry_run=router.dry_run,
        quarantine_dir=router.quarantine_dir,
        failed_dir=router.failed_dir,
        atomic_move=router._atomic_move,
        clear_retry_meta=router._clear_retry_meta,
        record_event=router._record_event,
        safe_journal=router._safe_journal,
    )
    router._ops = BrokerRouterOpsSurface(
        pending_dir=router.pending_dir,
        state_root=router.state_root,
        heartbeat_path=router.heartbeat_path,
        gate_pre_trade_path=router.gate_pre_trade_path,
        gate_heat_state_path=router.gate_heat_state_path,
        gate_journal_path=router.gate_journal_path,
        dry_run=router.dry_run,
        interval_s=router.interval_s,
        max_retries=router.max_retries,
        counts=router._counts,
        recent_events=router._recent_events,
        order_entry_hold=router._order_entry_hold,
        venue_circuit_states=router.venue_circuit_states,
        write_sidecar=router._write_sidecar,
        env_int=env_int,
        env_float=env_float,
        logger=logger,
    )
    router._reporting = BrokerRouterReporting(
        recent_events=router._recent_events,
        journal=router.journal,
        logger=logger,
    )
    router._gates = BrokerRouterGateEvaluator(
        heartbeat_path=router.heartbeat_path,
        gate_pre_trade_path=router.gate_pre_trade_path,
        gate_heat_state_path=router.gate_heat_state_path,
        gate_journal_path=router.gate_journal_path,
        normalize_gate_result=router._normalize_gate_result,
        load_build_default_chain=load_build_default_chain,
        gate_bootstrap_enabled=gate_bootstrap_enabled,
        order_entry_hold=router._order_entry_hold,
        sync_gate_state=router._sync_gate_state,
        readiness_enforced=readiness_enforced,
        readiness_snapshot_path=readiness_snapshot_path,
        live_money_env=live_money_env,
        logger=logger,
    )
    router._submission = BrokerRouterSubmission(
        counts=router._counts,
        retry_counts=router._retry_counts,
        max_retries=router.max_retries,
        archive_dir=router.archive_dir,
        fill_results_dir=router.fill_results_dir,
        place_with_failover_chain=router._place_with_failover_chain,
        handle_routing_error=router._handle_routing_error,
        write_sidecar=router._write_sidecar,
        move_to_failed_with_meta=router._move_to_failed_with_meta,
        save_retry_meta=router._save_retry_meta,
        clear_retry_meta=router._clear_retry_meta,
        record_event=router._record_event,
        safe_journal=router._safe_journal,
        atomic_move=router._atomic_move,
        extract_broker_fill_ts=extract_broker_fill_ts,
        logger=logger,
    )
    router._screening = BrokerRouterScreening(
        counts=router._counts,
        dry_run=router.dry_run,
        quarantine_dir=router.quarantine_dir,
        blocked_dir=router.blocked_dir,
        parse_pending_file=parse_pending_file,
        pending_order_sanity_denial=pending_order_sanity_denial,
        readiness_denial=router._readiness_denial,
        daily_loss_killswitch_denial=daily_loss_killswitch_denial,
        atomic_move=router._atomic_move,
        clear_retry_meta=router._clear_retry_meta,
        write_sidecar=router._write_sidecar,
        record_event=router._record_event,
        safe_journal=router._safe_journal,
        handle_processing_error=router._handle_processing_error,
        logger=logger,
    )
    router._resolution = BrokerRouterResolution(
        routing_config=router.routing_config,
        smart_router=router.smart_router,
        dry_run=router.dry_run,
        dormant_brokers=DORMANT_BROKERS,
        resolve_venue_adapter=router._resolve_venue_adapter,
        resolve_prop_account_venue=router._resolve_prop_account_venue,
        handle_blocked=router._handle_blocked,
        handle_routing_config_unsupported=router._handle_routing_config_unsupported,
        handle_routing_error=router._handle_routing_error,
        handle_dormant_broker=router._handle_dormant_broker,
        prop_order_risk_denial=prop_order_risk_denial,
        safe_journal=router._safe_journal,
        logger=logger,
    )
    router._polling = BrokerRouterPolling(
        pending_dir=router.pending_dir,
        processing_dir=router.processing_dir,
        dry_run=router.dry_run,
        counts=router._counts,
        order_entry_hold=router._order_entry_hold,
        emit_heartbeat=router._emit_heartbeat,
        record_event=router._record_event,
        process_pending_file=router._process_pending_file,
        process_retry_file=router._process_retry_file,
        parse_pending_file=parse_pending_file,
        routing_venue_for=lambda bot_id, symbol: router.routing_config.venue_for(bot_id, symbol=symbol),
        asset_class_for_symbol=asset_class_for_symbol,
        logger=logger,
    )
    router._runtime = BrokerRouterRuntimeControl(
        pending_dir=router.pending_dir,
        state_root=router.state_root,
        dry_run=router.dry_run,
        interval_s=router.interval_s,
        is_stopped=lambda: router._stopped,
        set_stopped=lambda value: setattr(router, "_stopped", bool(value)),
        tick=router._tick,
        logger=logger,
    )
    router._lifecycle = BrokerRouterLifecycleDriver(
        dry_run=router.dry_run,
        processing_dir=router.processing_dir,
        retry_meta_suffix=retry_meta_suffix,
        max_retries=router.max_retries,
        interval_s=router.interval_s,
        backoff_cap_s=backoff_cap_s,
        counts=router._counts,
        empty_retry_meta=lambda: EMPTY_RETRY_META.copy(),
        hold_blocks_file=router._hold_blocks_file,
        atomic_move=router._atomic_move,
        load_retry_meta=router._load_retry_meta,
        move_to_failed_with_meta=router._move_to_failed_with_meta,
        record_event=router._record_event,
        run_lifecycle=router._run_lifecycle,
        logger=logger,
    )
