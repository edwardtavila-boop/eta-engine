from __future__ import annotations

from pathlib import Path

from eta_engine.scripts import workspace_roots

ROOT = Path(__file__).resolve().parents[2]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_workspace_roots_point_inside_canonical_repo() -> None:
    assert workspace_roots.WORKSPACE_ROOT == ROOT
    assert workspace_roots.MNQ_DATA_ROOT == ROOT / "mnq_data"
    assert workspace_roots.MNQ_HISTORY_ROOT == ROOT / "mnq_data" / "history"
    assert workspace_roots.CRYPTO_HISTORY_ROOT == ROOT / "data" / "crypto" / "history"
    assert workspace_roots.CRYPTO_IBKR_HISTORY_ROOT == ROOT / "data" / "crypto" / "ibkr" / "history"
    assert workspace_roots.CRYPTO_MACRO_ROOT == ROOT / "data" / "crypto" / "macro"
    assert workspace_roots.ETA_RUNTIME_STATE_DIR == ROOT / "var" / "eta_engine" / "state"
    assert workspace_roots.ETA_RUNTIME_LOG_DIR == ROOT / "logs" / "eta_engine"
    assert workspace_roots.ETA_RUNTIME_HEALTH_DIR == ROOT / "var" / "eta_engine" / "state" / "health"
    assert workspace_roots.ETA_BOT_STATE_ROOT == ROOT / "var" / "eta_engine" / "state"
    assert workspace_roots.ETA_EVENT_CALENDAR_PATH == ROOT / "var" / "eta_engine" / "state" / "event_calendar.yaml"
    assert workspace_roots.ETA_FM_TRADE_GATES_LOG_PATH == ROOT / "var" / "eta_engine" / "state" / "fm_trade_gates.jsonl"
    assert workspace_roots.ETA_IDEMPOTENCY_STORE_PATH == ROOT / "var" / "eta_engine" / "state" / "idempotency.jsonl"
    assert workspace_roots.ETA_KAIZEN_LEDGER_PATH == ROOT / "var" / "eta_engine" / "state" / "kaizen_ledger.json"
    assert workspace_roots.ETA_KAIZEN_LEDGER_JSONL_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "kaizen_ledger.jsonl"
    )
    assert workspace_roots.ETA_KAIZEN_REPORT_DIR == ROOT / "var" / "eta_engine" / "state" / "kaizen_reports"
    assert workspace_roots.ETA_KAIZEN_ACTIONS_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "kaizen_actions.jsonl"
    )
    assert workspace_roots.ETA_KAIZEN_OVERRIDES_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "kaizen_overrides.json"
    )
    assert workspace_roots.ETA_KAIZEN_REACTIVATE_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "kaizen_reactivate.log"
    )
    assert workspace_roots.ETA_PAPER_SOAK_LEDGER_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "paper_soak_ledger.json"
    )
    assert workspace_roots.ETA_CAPITAL_ALLOCATION_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "capital_allocation.json"
    )
    assert workspace_roots.ETA_INVESTOR_DASHBOARD_PATH == (
        ROOT / "var" / "eta_engine" / "investor_dashboard" / "index.html"
    )
    assert workspace_roots.ETA_NOTION_EXPORT_DIR == ROOT / "var" / "eta_engine" / "notion_export"
    assert workspace_roots.ETA_JARVIS_AUDIT_DIR == ROOT / "var" / "eta_engine" / "state" / "jarvis_audit"
    assert workspace_roots.ETA_KAIZEN_CRITIQUE_DIR == ROOT / "var" / "eta_engine" / "state" / "kaizen_critique"
    assert workspace_roots.ETA_BANDIT_PROMOTION_DIR == ROOT / "var" / "eta_engine" / "state" / "bandit"
    assert workspace_roots.ETA_MODEL_ARTIFACT_DIR == ROOT / "var" / "eta_engine" / "state" / "models"
    assert workspace_roots.ETA_CORRELATION_ARTIFACT_DIR == ROOT / "var" / "eta_engine" / "state" / "correlation"
    assert workspace_roots.ETA_CORRELATION_REGIME_DIR == ROOT / "var" / "eta_engine" / "state" / "correlation_regime"
    assert workspace_roots.ETA_ANOMALY_ALERT_STATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "anomaly" / "last_alert.json"
    )
    assert workspace_roots.ETA_CALIBRATION_MODEL_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "calibration" / "platt_sigmoid.json"
    )
    assert workspace_roots.ETA_CALIBRATOR_LABELS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "calibrator_labels.jsonl"
    )
    assert workspace_roots.ETA_HOT_LEARNER_STATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "hot_learner.json"
    )
    assert workspace_roots.ETA_FLEET_STATE_PATH == ROOT / "var" / "eta_engine" / "state" / "fleet_state.json"
    assert workspace_roots.ETA_JARVIS_TRACE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_trace.jsonl"
    )
    assert workspace_roots.ETA_JARVIS_WIRING_AUDIT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_wiring_audit.json"
    )
    assert workspace_roots.ETA_KAIZEN_LATEST_PATH == ROOT / "var" / "eta_engine" / "state" / "kaizen_latest.json"
    assert workspace_roots.ETA_REGIME_STATE_PATH == ROOT / "var" / "eta_engine" / "state" / "regime_state.json"
    assert workspace_roots.ETA_AGENT_REGISTRY_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "agent_registry.json"
    )
    assert workspace_roots.ETA_HERMES_OVERRIDES_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "hermes_overrides.json"
    )
    assert workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "hermes_actions.jsonl"
    )
    assert workspace_roots.ETA_HERMES_MEMORY_DB_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "hermes_memory_store.db"
    )
    assert workspace_roots.ETA_HERMES_MEMORY_BACKUP_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "backups" / "hermes_memory"
    )
    assert workspace_roots.ETA_SENTIMENT_CACHE_DIR == ROOT / "var" / "eta_engine" / "state" / "sentiment"
    assert workspace_roots.ETA_TRADE_JOURNAL_DIR == ROOT / "var" / "eta_engine" / "state" / "trade_journal"
    assert workspace_roots.ETA_VERDICT_WEBHOOK_CURSOR_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "verdict_webhook" / "cursor.json"
    )
    assert workspace_roots.ETA_JARVIS_DENIAL_RATE_ALERT_STATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_denial_rate_state.json"
    )
    assert workspace_roots.ETA_ANOMALY_HITS_LOG_PATH == ROOT / "var" / "eta_engine" / "state" / "anomaly_watcher.jsonl"
    assert workspace_roots.ETA_TELEGRAM_INBOUND_OFFSET_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "telegram_inbound_offset.json"
    )
    assert workspace_roots.ETA_TELEGRAM_SILENCE_UNTIL_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "telegram_silence_until.json"
    )
    assert workspace_roots.ETA_TELEGRAM_HERMES_LAST_CHAT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "telegram_hermes_last_chat.json"
    )
    assert workspace_roots.ETA_HERMES_PROACTIVE_CURSOR_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "hermes_proactive_cursor.json"
    )
    assert workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR == (ROOT / "var" / "eta_engine" / "state" / "research_grid")
    assert workspace_roots.ETA_LIVE_DATA_RUNTIME_DIR == (ROOT / "var" / "eta_engine" / "state" / "live_data")
    assert workspace_roots.ETA_TRADINGVIEW_AUTH_STATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "tradingview_auth.json"
    )
    assert workspace_roots.ETA_TRADINGVIEW_DATA_ROOT == (
        ROOT / "var" / "eta_engine" / "state" / "live_data" / "tradingview"
    )
    assert workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "operator_queue_snapshot.json"
    )
    assert workspace_roots.ETA_OPERATOR_QUEUE_PREVIOUS_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "operator_queue_snapshot.previous.json"
    )
    assert workspace_roots.ETA_FLAW_HARDENING_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "flaw_hardening_snapshot.json"
    )
    assert workspace_roots.ETA_FLAW_HARDENING_PREVIOUS_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "flaw_hardening_snapshot.previous.json"
    )
    assert workspace_roots.ETA_IBC_CUTOVER_READINESS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "ibc_cutover_readiness.json"
    )
    assert workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "bot_strategy_readiness_latest.json"
    )
    assert workspace_roots.ETA_PAPER_LIVE_LAUNCH_CHECK_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "paper_live_launch_check_latest.json"
    )
    assert workspace_roots.ETA_PROP_LIVE_READINESS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "prop_live_readiness_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_LEADERBOARD_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_leaderboard_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_PROP_LAUNCH_READINESS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_prop_launch_readiness_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_WATCHDOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_watchdog_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_DEMOTION_GATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_demotion_gate_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_DIRECTION_STRATIFY_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_direction_stratify_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_FEED_SANITY_AUDIT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_feed_sanity_audit_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_PROMOTION_GATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_promotion_gate_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_SIZING_AUDIT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_sizing_audit_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_OPS_DASHBOARD_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_ops_dashboard_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_PROP_ALLOCATOR_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_prop_allocator_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_PROP_DRAWDOWN_GUARD_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_prop_drawdown_guard_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_RETUNE_STATUS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_retune_status_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_PROP_ALERT_CURSOR_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_prop_alert_cursor.json"
    )
    assert workspace_roots.ETA_DIAMOND_PROP_ALERT_DISPATCHER_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_prop_alert_dispatcher_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_QTY_ASYMMETRY_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_qty_asymmetry_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_LIVE_PAPER_DRIFT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_live_paper_drift_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_PRESET_VALIDATION_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_preset_validation_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_AUTHENTICITY_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_authenticity_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_PROP_PRELAUNCH_DRYRUN_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_prop_prelaunch_dryrun_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_WAVE25_STATUS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_wave25_status_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_CPCV_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_cpcv_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_SANITIZER_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_sanitizer_latest.json"
    )
    assert workspace_roots.ETA_DIAMOND_REGIME_STRATIFY_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "diamond_regime_stratify_latest.json"
    )
    assert workspace_roots.ETA_PROP_HALT_FLAG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "prop_halt_active.flag"
    )
    assert workspace_roots.ETA_PROP_WATCH_FLAG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "prop_watch_active.flag"
    )
    assert workspace_roots.ETA_PUBLIC_BROKER_CLOSE_TRUTH_CACHE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "health" / "public_broker_close_truth_latest.json"
    )
    assert workspace_roots.ETA_L2_BACKTEST_RUNS_LOG_PATH == ROOT / "logs" / "eta_engine" / "l2_backtest_runs.jsonl"
    assert workspace_roots.ETA_DIAMOND_AUTHENTICITY_LOG_PATH == (
        ROOT / "logs" / "eta_engine" / "diamond_authenticity.jsonl"
    )
    assert workspace_roots.ETA_JARVIS_V3_EVENTS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_v3_events.jsonl"
    )
    assert workspace_roots.ETA_ETA_EVENTS_LOG_PATH == ROOT / "var" / "eta_engine" / "state" / "eta_events.jsonl"
    assert workspace_roots.ETA_ETA_ALERT_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "eta_alert_snapshot.json"
    )
    assert workspace_roots.ETA_TWS_WATCHDOG_STATUS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "tws_watchdog.json"
    )
    assert workspace_roots.ETA_CUTOVER_STATUS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "cutover_status.json"
    )
    assert workspace_roots.ETA_MULTI_MODEL_TELEMETRY_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "multi_model_telemetry.jsonl"
    )
    assert workspace_roots.ETA_JARVIS_INTEL_STATE_DIR == ROOT / "var" / "eta_engine" / "state" / "jarvis_intel"
    assert workspace_roots.ETA_JARVIS_DAILY_BRIEF_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "daily_briefs"
    )
    assert workspace_roots.ETA_JARVIS_POSTMORTEM_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "postmortems"
    )
    assert workspace_roots.ETA_HERMES_STATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "hermes_state.json"
    )
    assert workspace_roots.ETA_JARVIS_SUPERVISOR_STATE_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "supervisor"
    )
    assert workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "supervisor" / "heartbeat.json"
    )
    assert workspace_roots.ETA_JARVIS_SUPERVISOR_RECONCILE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "supervisor" / "reconcile_last.json"
    )
    assert workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"
    )
    assert workspace_roots.ETA_CLOSED_TRADE_LEDGER_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "closed_trade_ledger_latest.json"
    )
    assert workspace_roots.ETA_BROKER_BRACKET_AUDIT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "broker_bracket_audit_latest.json"
    )
    assert workspace_roots.ETA_BROKER_BRACKET_MANUAL_ACK_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "broker_bracket_manual_oco_ack.json"
    )
    assert workspace_roots.ETA_BROKER_ROUTER_FILLS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "broker_router_fills.jsonl"
    )
    assert workspace_roots.ETA_PROP_OPERATOR_CHECKLIST_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "prop_operator_checklist_latest.json"
    )
    assert workspace_roots.ETA_PROP_STRATEGY_PROMOTION_AUDIT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "prop_strategy_promotion_audit_latest.json"
    )
    assert workspace_roots.ETA_BOT_LIFECYCLE_STATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "bot_lifecycle.json"
    )
    assert workspace_roots.ETA_DRIFT_WATCHDOG_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "drift_watchdog.jsonl"
    )
    assert workspace_roots.ETA_JARVIS_DRIFT_JOURNAL_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_drift.jsonl"
    )
    assert workspace_roots.ETA_SHARED_BREAKER_STATE_PATH == (ROOT / "var" / "eta_engine" / "state" / "breaker.json")
    assert workspace_roots.ETA_DEADMAN_SENTINEL_PATH == (ROOT / "var" / "eta_engine" / "state" / "operator.sentinel")
    assert workspace_roots.ETA_DEADMAN_JOURNAL_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "operator_activity.jsonl"
    )
    assert workspace_roots.ETA_PROMOTION_STATE_PATH == (ROOT / "var" / "eta_engine" / "state" / "promotion.json")
    assert workspace_roots.ETA_PROMOTION_JOURNAL_PATH == (ROOT / "var" / "eta_engine" / "state" / "promotion.jsonl")
    assert workspace_roots.ETA_AVENGERS_JOURNAL_PATH == (ROOT / "var" / "eta_engine" / "state" / "avengers.jsonl")
    assert workspace_roots.ETA_CALIBRATION_JOURNAL_PATH == (ROOT / "var" / "eta_engine" / "state" / "calibration.jsonl")
    assert workspace_roots.ETA_AVENGER_DAEMON_PID_DIR == (ROOT / "var" / "eta_engine" / "state" / "avenger_daemons")
    # B-class state writers migrated 2026-05-04 (LEGACY_PATH_AUDIT.md
    # category B). Each writer's canonical target is the workspace var/
    # tree; the legacy in-repo path is captured here so the read-fallback
    # window is auditable in test fixtures rather than scattered across
    # callers.
    assert workspace_roots.ETA_KILL_SWITCH_LATCH_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "kill_switch_latch.json"
    )
    assert workspace_roots.ETA_TRAILING_DD_TRACKER_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "trailing_dd_tracker.json"
    )
    assert workspace_roots.ETA_LEGACY_KILL_SWITCH_LATCH_PATH == (
        ROOT / "eta_engine" / "state" / "kill_switch_latch.json"
    )
    assert workspace_roots.ETA_LEGACY_TRAILING_DD_TRACKER_PATH == (
        ROOT / "eta_engine" / "state" / "trailing_dd_tracker.json"
    )
    assert workspace_roots.ETA_FM_HEALTH_SNAPSHOT_PATH == (ROOT / "var" / "eta_engine" / "state" / "fm_health.json")
    assert workspace_roots.ETA_LEGACY_FM_HEALTH_SNAPSHOT_PATH == (ROOT / "eta_engine" / "state" / "fm_health.json")
    assert workspace_roots.ETA_JARVIS_VERDICTS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "verdicts.jsonl"
    )
    assert workspace_roots.ETA_LEGACY_JARVIS_VERDICTS_PATH == (
        ROOT / "eta_engine" / "state" / "jarvis_intel" / "verdicts.jsonl"
    )
    assert workspace_roots.ETA_LEGACY_JARVIS_INTEL_STATE_DIR == ROOT / "eta_engine" / "state" / "jarvis_intel"
    assert workspace_roots.ETA_LEGACY_JARVIS_DAILY_BRIEF_DIR == (
        ROOT / "eta_engine" / "state" / "jarvis_intel" / "daily_briefs"
    )
    assert workspace_roots.ETA_LEGACY_JARVIS_POSTMORTEM_DIR == (
        ROOT / "eta_engine" / "state" / "jarvis_intel" / "postmortems"
    )
    assert workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH == (
        ROOT / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"
    )
    assert workspace_roots.ETA_LEGACY_JARVIS_AUDIT_DIR == ROOT / "eta_engine" / "state" / "jarvis_audit"
    assert workspace_roots.ETA_LEGACY_KAIZEN_CRITIQUE_DIR == ROOT / "eta_engine" / "state" / "kaizen_critique"
    assert workspace_roots.ETA_LEGACY_BANDIT_PROMOTION_DIR == ROOT / "eta_engine" / "state" / "bandit"
    assert workspace_roots.ETA_LEGACY_MODEL_ARTIFACT_DIR == ROOT / "eta_engine" / "state" / "models"
    assert workspace_roots.ETA_LEGACY_CORRELATION_ARTIFACT_DIR == ROOT / "eta_engine" / "state" / "correlation"
    assert workspace_roots.ETA_LEGACY_CORRELATION_REGIME_DIR == ROOT / "eta_engine" / "state" / "correlation_regime"
    assert workspace_roots.ETA_LEGACY_ANOMALY_ALERT_STATE_PATH == (
        ROOT / "eta_engine" / "state" / "anomaly" / "last_alert.json"
    )
    assert workspace_roots.ETA_LEGACY_VERDICT_WEBHOOK_CURSOR_PATH == (
        ROOT / "eta_engine" / "state" / "verdict_webhook" / "cursor.json"
    )
    assert workspace_roots.ETA_LEGACY_JARVIS_DENIAL_RATE_ALERT_STATE_PATH == (
        ROOT / "eta_engine" / "var" / "alerter" / "jarvis_denial_rate_state.json"
    )
    assert workspace_roots.ETA_EVAL_PROMPTFOO_RESULTS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "eval" / "promptfoo_results.json"
    )
    assert workspace_roots.ETA_LEGACY_EVAL_PROMPTFOO_RESULTS_PATH == (
        ROOT / "eta_engine" / "state" / "eval" / "promptfoo_results.json"
    )
    assert workspace_roots.ETA_HERMES_KILL_LATCH_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "kill_switch_latch.json"
    )
    assert workspace_roots.ETA_LEGACY_HERMES_KILL_LATCH_PATH == (
        ROOT / "eta_engine" / "state" / "kill_switch_latch.json"
    )
    assert workspace_roots.ETA_AVENGER_METRICS_PATH == ROOT / "logs" / "eta_engine" / "metrics.prom"
    assert workspace_roots.ETA_ANOMALY_PULSE_LOG_PATH == ROOT / "logs" / "eta_engine" / "anomaly_pulse.jsonl"
    assert workspace_roots.ETA_TELEGRAM_INBOUND_AUDIT_LOG_PATH == (
        ROOT / "logs" / "eta_engine" / "telegram_inbound.jsonl"
    )
    assert workspace_roots.ETA_HERMES_PROACTIVE_AUDIT_PATH == (
        ROOT / "logs" / "eta_engine" / "hermes_proactive_audit.jsonl"
    )
    assert workspace_roots.ETA_HERMES_VOICE_LOG_PATH == ROOT / "logs" / "eta_engine" / "hermes_voice.log"
    assert workspace_roots.ETA_JARVIS_LIVE_HEALTH_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "jarvis_live_health.json"
    )
    assert workspace_roots.ETA_JARVIS_LIVE_LOG_PATH == ROOT / "var" / "eta_engine" / "state" / "jarvis_live_log.jsonl"
    assert workspace_roots.ETA_BTC_PAPER_STATE_DIR == ROOT / "var" / "eta_engine" / "state" / "btc_paper"
    assert workspace_roots.ETA_BTC_PAPER_RUN_LATEST_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "btc_paper" / "btc_paper_run_latest.json"
    )
    assert workspace_roots.ETA_BTC_LIVE_STATE_DIR == ROOT / "var" / "eta_engine" / "state" / "btc_live"
    assert workspace_roots.ETA_BTC_LIVE_DECISIONS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "btc_live" / "btc_live_decisions.jsonl"
    )
    assert workspace_roots.ETA_BTC_BROKER_FLEET_STATE_DIR == ROOT / "var" / "eta_engine" / "state" / "broker_fleet"
    assert workspace_roots.ETA_BROKER_CONNECTION_REPORT_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "broker_connections"
    )
    assert workspace_roots.ETA_MNQ_LIVE_STATE_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "mnq_live"
    )
    assert workspace_roots.ETA_INTEGRATIONS_REPORT_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "integrations"
    )
    assert workspace_roots.ETA_INTEGRATIONS_LIVE_STATUS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "integrations" / "integrations_live_status.json"
    )
    assert workspace_roots.ETA_MONTHLY_REVIEW_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "monthly_review"
    )
    assert workspace_roots.ETA_WEEKLY_REVIEW_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "weekly_review"
    )
    assert workspace_roots.ETA_FIRM_BOARD_TEMP_SPEC_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "firm_board" / "_firm_spec_temp.json"
    )
    assert workspace_roots.ETA_KILL_LOG_PATH == ROOT / "var" / "eta_engine" / "state" / "kill_log.json"
    assert workspace_roots.ETA_WEEKLY_REVIEW_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "weekly_review" / "weekly_review_log.json"
    )
    assert workspace_roots.ETA_WEEKLY_REVIEW_LATEST_JSON_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "weekly_review" / "weekly_review_latest.json"
    )
    assert workspace_roots.ETA_WEEKLY_REVIEW_LATEST_TXT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "weekly_review" / "weekly_review_latest.txt"
    )
    assert workspace_roots.ETA_WEEKLY_CHECKLIST_TEMPLATE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "weekly_review" / "weekly_checklist_template.json"
    )
    assert workspace_roots.ETA_WEEKLY_CHECKLIST_LATEST_JSON_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "weekly_review" / "weekly_checklist_latest.json"
    )
    assert workspace_roots.ETA_WEEKLY_CHECKLIST_LATEST_TXT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "weekly_review" / "weekly_checklist_latest.txt"
    )
    assert workspace_roots.ETA_PREMARKET_INPUTS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "premarket_inputs.json"
    )
    assert workspace_roots.ETA_PREMARKET_REPORT_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "premarket"
    )
    assert workspace_roots.ETA_PAPER_RUN_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "paper_run"
    )
    assert workspace_roots.ETA_PAPER_RUN_REPORT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "paper_run" / "paper_run_report.json"
    )
    assert workspace_roots.ETA_PAPER_RUN_TEARSHEET_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "paper_run" / "paper_run_tearsheet.txt"
    )
    assert workspace_roots.ETA_PREFLIGHT_DRYRUN_DIR == (
        ROOT / "var" / "eta_engine" / "state" / "preflight"
    )
    assert workspace_roots.ETA_PREFLIGHT_DRYRUN_REPORT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "preflight" / "preflight_dryrun_report.json"
    )
    assert workspace_roots.ETA_PREFLIGHT_DRYRUN_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "preflight" / "preflight_dryrun_log.txt"
    )
    assert workspace_roots.ETA_GO_TRIGGER_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "go_trigger_log.jsonl"
    )
    assert workspace_roots.ETA_LEGACY_BROKER_CONNECTION_REPORT_DIR == (
        ROOT / "eta_engine" / "docs" / "broker_connections"
    )
    assert workspace_roots.ETA_LEGACY_MNQ_LIVE_STATE_DIR == (
        ROOT / "eta_engine" / "docs" / "mnq_live"
    )
    assert workspace_roots.ETA_LEGACY_INTEGRATIONS_REPORT_DIR == ROOT / "eta_engine" / "docs"
    assert workspace_roots.ETA_LEGACY_INTEGRATIONS_LIVE_STATUS_PATH == (
        ROOT / "eta_engine" / "docs" / "integrations_live_status.json"
    )
    assert workspace_roots.ETA_LEGACY_MONTHLY_REVIEW_DIR == ROOT / "eta_engine" / "docs"
    assert workspace_roots.ETA_LEGACY_WEEKLY_REVIEW_DIR == ROOT / "eta_engine" / "docs"
    assert workspace_roots.ETA_LEGACY_KILL_LOG_PATH == ROOT / "eta_engine" / "docs" / "kill_log.json"
    assert workspace_roots.ETA_LEGACY_WEEKLY_REVIEW_LOG_PATH == (
        ROOT / "eta_engine" / "docs" / "weekly_review_log.json"
    )
    assert workspace_roots.ETA_LEGACY_WEEKLY_REVIEW_LATEST_JSON_PATH == (
        ROOT / "eta_engine" / "docs" / "weekly_review_latest.json"
    )
    assert workspace_roots.ETA_LEGACY_WEEKLY_REVIEW_LATEST_TXT_PATH == (
        ROOT / "eta_engine" / "docs" / "weekly_review_latest.txt"
    )
    assert workspace_roots.ETA_LEGACY_WEEKLY_CHECKLIST_TEMPLATE_PATH == (
        ROOT / "eta_engine" / "docs" / "weekly_checklist_template.json"
    )
    assert workspace_roots.ETA_LEGACY_WEEKLY_CHECKLIST_LATEST_JSON_PATH == (
        ROOT / "eta_engine" / "docs" / "weekly_checklist_latest.json"
    )
    assert workspace_roots.ETA_LEGACY_WEEKLY_CHECKLIST_LATEST_TXT_PATH == (
        ROOT / "eta_engine" / "docs" / "weekly_checklist_latest.txt"
    )
    assert workspace_roots.ETA_LEGACY_PREMARKET_INPUTS_PATH == (
        ROOT / "eta_engine" / "docs" / "premarket_inputs.json"
    )
    assert workspace_roots.ETA_LEGACY_PREMARKET_REPORT_DIR == ROOT / "eta_engine" / "docs"
    assert workspace_roots.ETA_LEGACY_PAPER_RUN_DIR == ROOT / "eta_engine" / "docs"
    assert workspace_roots.ETA_LEGACY_PAPER_RUN_REPORT_PATH == (
        ROOT / "eta_engine" / "docs" / "paper_run_report.json"
    )
    assert workspace_roots.ETA_LEGACY_PAPER_RUN_TEARSHEET_PATH == (
        ROOT / "eta_engine" / "docs" / "paper_run_tearsheet.txt"
    )
    assert workspace_roots.ETA_LEGACY_PREFLIGHT_DRYRUN_REPORT_PATH == (
        ROOT / "eta_engine" / "docs" / "preflight_dryrun_report.json"
    )
    assert workspace_roots.ETA_LEGACY_PREFLIGHT_DRYRUN_LOG_PATH == (
        ROOT / "eta_engine" / "docs" / "preflight_dryrun_log.txt"
    )
    assert workspace_roots.ETA_LEGACY_GO_TRIGGER_LOG_PATH == (
        ROOT / "eta_engine" / "docs" / "go_trigger_log.jsonl"
    )
    assert workspace_roots.ETA_DECISIONS_V1_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "decisions_v1.json"
    )
    assert workspace_roots.ETA_SHARPE_BASELINE_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "sharpe_baseline.json"
    )
    assert workspace_roots.ETA_LEGACY_DECISIONS_V1_PATH == (
        ROOT / "eta_engine" / "docs" / "decisions_v1.json"
    )
    assert workspace_roots.ETA_LEGACY_SHARPE_BASELINE_PATH == (
        ROOT / "eta_engine" / "docs" / "sharpe_baseline.json"
    )
    assert workspace_roots.default_paper_run_report_path() in {
        ROOT / "var" / "eta_engine" / "state" / "paper_run" / "paper_run_report.json",
        ROOT / "eta_engine" / "docs" / "paper_run_report.json",
    }
    assert workspace_roots.default_decisions_v1_path() in {
        ROOT / "var" / "eta_engine" / "state" / "decisions_v1.json",
        ROOT / "eta_engine" / "docs" / "decisions_v1.json",
    }
    assert workspace_roots.default_sharpe_baseline_path() in {
        ROOT / "var" / "eta_engine" / "state" / "sharpe_baseline.json",
        ROOT / "eta_engine" / "docs" / "sharpe_baseline.json",
    }
    assert workspace_roots.default_premarket_inputs_path() in {
        ROOT / "var" / "eta_engine" / "state" / "premarket_inputs.json",
        ROOT / "eta_engine" / "docs" / "premarket_inputs.json",
    }
    assert workspace_roots.default_weekly_review_latest_path() in {
        ROOT / "var" / "eta_engine" / "state" / "weekly_review" / "weekly_review_latest.json",
        ROOT / "eta_engine" / "docs" / "weekly_review_latest.json",
    }
    assert workspace_roots.default_kill_log_path() in {
        ROOT / "var" / "eta_engine" / "state" / "kill_log.json",
        ROOT / "eta_engine" / "docs" / "kill_log.json",
    }
    assert workspace_roots.default_preflight_dryrun_report_path() in {
        ROOT / "var" / "eta_engine" / "state" / "preflight" / "preflight_dryrun_report.json",
        ROOT / "eta_engine" / "docs" / "preflight_dryrun_report.json",
    }
    assert workspace_roots.ETA_LEGACY_SHARED_BREAKER_STATE_PATH.name == "breaker.json"
    assert workspace_roots.ETA_LEGACY_SHARED_BREAKER_STATE_PATH.parent.name == ".jarvis"
    assert workspace_roots.ETA_LEGACY_DEADMAN_SENTINEL_PATH.name == "operator.sentinel"
    assert workspace_roots.ETA_LEGACY_DEADMAN_SENTINEL_PATH.parent.name == ".jarvis"
    assert workspace_roots.ETA_LEGACY_PROMOTION_STATE_PATH.name == "promotion.json"
    assert workspace_roots.ETA_LEGACY_PROMOTION_STATE_PATH.parent.name == ".jarvis"
    assert workspace_roots.ETA_LEGACY_AVENGERS_JOURNAL_PATH.name == "avengers.jsonl"
    assert workspace_roots.ETA_LEGACY_AVENGERS_JOURNAL_PATH.parent.name == ".jarvis"
    assert workspace_roots.ETA_LEGACY_CALIBRATION_JOURNAL_PATH.name == "calibration.jsonl"
    assert workspace_roots.ETA_LEGACY_CALIBRATION_JOURNAL_PATH.parent.name == ".jarvis"
    assert workspace_roots.ETA_RUNTIME_ALERTS_LOG_PATH == ROOT / "logs" / "eta_engine" / "alerts_log.jsonl"
    assert workspace_roots.ETA_RUNTIME_LOG_PATH == ROOT / "logs" / "eta_engine" / "runtime_log.jsonl"
    assert workspace_roots.ETA_LEGACY_DOCS_DRIFT_WATCHDOG_LOG_PATH == (
        ROOT / "eta_engine" / "docs" / "drift_watchdog.jsonl"
    )
    assert workspace_roots.ETA_LEGACY_DOCS_ALERTS_LOG_PATH == ROOT / "eta_engine" / "docs" / "alerts_log.jsonl"
    assert workspace_roots.ETA_LEGACY_DOCS_RUNTIME_LOG_PATH == ROOT / "eta_engine" / "docs" / "runtime_log.jsonl"


def test_targeted_scripts_drop_legacy_absolute_data_paths() -> None:
    targets = (
        "eta_engine/scripts/data_pipeline/extract_mnq.py",
        "eta_engine/scripts/data_pipeline/pull_tv_bars.py",
        "eta_engine/scripts/investigate_window_0.py",
        "eta_engine/scripts/paper_live_launch_check.py",
        "eta_engine/scripts/run_btc_feature_regime_walk_forward.py",
        "eta_engine/scripts/run_btc_regime_gated_walk_forward.py",
        "eta_engine/scripts/run_btc_supercharge_walk_forward.py",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert r"C:\mnq_data" not in text
        assert r"C:\crypto_data" not in text


def test_second_path_cleanup_wave_uses_workspace_root_helpers() -> None:
    targets = (
        "eta_engine/scripts/compare_coinbase_vs_ibkr.py",
        "eta_engine/scripts/extend_nq_daily_yahoo.py",
        "eta_engine/scripts/fetch_btc_bars.py",
        "eta_engine/scripts/fetch_btc_funding_extended.py",
        "eta_engine/scripts/fetch_btc_open_interest.py",
        "eta_engine/scripts/fetch_etf_flows_farside.py",
        "eta_engine/scripts/fetch_eth_etf_flows_farside.py",
        "eta_engine/scripts/fetch_fear_greed_alternative.py",
        "eta_engine/scripts/fetch_funding_rates.py",
        "eta_engine/scripts/fetch_ibkr_crypto_bars.py",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert "workspace_roots" in text


def test_third_path_cleanup_wave_uses_workspace_root_helpers() -> None:
    targets = (
        "eta_engine/scripts/fetch_index_futures_bars.py",
        "eta_engine/scripts/fetch_lth_proxy.py",
        "eta_engine/scripts/fetch_market_context_bars.py",
        "eta_engine/scripts/fetch_onchain_history.py",
        "eta_engine/scripts/fetch_xrp_news_history.py",
        "eta_engine/scripts/resample_btc_timeframes.py",
        "eta_engine/scripts/run_funding_divergence_walk_forward.py",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert "workspace_roots" in text


def test_fourth_path_cleanup_wave_uses_workspace_root_helpers() -> None:
    targets = (
        "eta_engine/data/library.py",
        "eta_engine/deploy/scripts/run_task.py",
        "eta_engine/strategies/per_bot_registry.py",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert "workspace_roots" in text


def test_fifth_path_cleanup_wave_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/bots/base_bot.py": "workspace_roots.ETA_BOT_STATE_ROOT",
        "eta_engine/brain/fm_trade_gates.py": "workspace_roots.ETA_FM_TRADE_GATES_LOG_PATH",
        "eta_engine/data/event_calendar.py": "workspace_roots.ETA_EVENT_CALENDAR_PATH",
        "eta_engine/safety/idempotency.py": "workspace_roots.ETA_IDEMPOTENCY_STORE_PATH",
        "eta_engine/scripts/bandit_promotion_check.py": "workspace_roots.ETA_BANDIT_PROMOTION_DIR",
        "eta_engine/scripts/generate_investor_dashboard.py": "workspace_roots.ETA_KAIZEN_LEDGER_PATH",
        "eta_engine/scripts/export_to_notion.py": "workspace_roots.ETA_KAIZEN_LEDGER_PATH",
        "eta_engine/scripts/eta_live_preflight.py": "workspace_roots.ETA_KAIZEN_LEDGER_PATH",
        "eta_engine/scripts/run_kaizen_close_cycle.py": "workspace_roots.ETA_KAIZEN_LEDGER_PATH",
        "eta_engine/scripts/run_critique_nightly.py": "workspace_roots.ETA_KAIZEN_CRITIQUE_DIR",
        "eta_engine/obs/jarvis_today_verdicts.py": "workspace_roots.ETA_JARVIS_AUDIT_DIR",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    text = _read("eta_engine/bots/base_bot.py")
    assert r'C:/EvolutionaryTradingAlgo/var/eta_engine/state' not in text

    text = _read("eta_engine/brain/fm_trade_gates.py")
    assert r'C:\EvolutionaryTradingAlgo\var\eta_engine\state\fm_trade_gates.jsonl' not in text

    text = _read("eta_engine/data/event_calendar.py")
    assert r'C:\EvolutionaryTradingAlgo\var\eta_engine\state\event_calendar.yaml' not in text

    text = _read("eta_engine/safety/idempotency.py")
    assert '_DEFAULT_PERSIST_PATH: _Path = workspace_roots.ETA_IDEMPOTENCY_STORE_PATH' in text

    text = _read("eta_engine/scripts/generate_investor_dashboard.py")
    assert 'ROOT / "docs" / "kaizen_ledger.json"' not in text
    assert 'ROOT / "state" / "investor_dashboard"' not in text

    text = _read("eta_engine/scripts/export_to_notion.py")
    assert 'ROOT / "docs" / "kaizen_ledger.json"' not in text
    assert 'ROOT / "state" / "notion_export"' not in text
    assert 'ROOT / "state" / "jarvis_audit"' not in text
    assert 'ROOT / "state" / "kaizen_critique"' not in text
    assert 'ROOT / "state" / "bandit"' not in text

    text = _read("eta_engine/scripts/eta_live_preflight.py")
    assert 'ROOT / "docs" / "kaizen_ledger.json"' not in text
    assert 'ROOT / "docs" / "kaizen_ledger.jsonl"' not in text

    text = _read("eta_engine/scripts/run_kaizen_close_cycle.py")
    assert 'ROOT / "docs" / "kaizen_ledger.jsonl"' not in text

    text = _read("eta_engine/scripts/bandit_promotion_check.py")
    assert 'ROOT / "state" / "jarvis_audit"' not in text
    assert 'ROOT / "state" / "bandit"' not in text

    text = _read("eta_engine/scripts/run_critique_nightly.py")
    assert 'ROOT / "state" / "jarvis_audit"' not in text
    assert 'ROOT / "state" / "kaizen_critique"' not in text


def test_diamond_path_cleanup_wave_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/scripts/diamond_demotion_gate.py": "workspace_roots.ETA_DIAMOND_DEMOTION_GATE_PATH",
        "eta_engine/scripts/diamond_direction_stratify.py": "workspace_roots.ETA_DIAMOND_DIRECTION_STRATIFY_PATH",
        "eta_engine/scripts/diamond_falsification_watchdog.py": "workspace_roots.ETA_DIAMOND_WATCHDOG_PATH",
        "eta_engine/scripts/diamond_feed_sanity_audit.py": "workspace_roots.ETA_DIAMOND_FEED_SANITY_AUDIT_PATH",
        "eta_engine/scripts/diamond_leaderboard.py": "workspace_roots.ETA_DIAMOND_LEADERBOARD_PATH",
        "eta_engine/scripts/diamond_ops_dashboard.py": "workspace_roots.ETA_DIAMOND_OPS_DASHBOARD_PATH",
        "eta_engine/scripts/diamond_promotion_gate.py": "workspace_roots.ETA_DIAMOND_PROMOTION_GATE_PATH",
        "eta_engine/scripts/diamond_prop_alert_dispatcher.py": "workspace_roots.ETA_DIAMOND_PROP_ALERT_CURSOR_PATH",
        "eta_engine/scripts/diamond_prop_allocator.py": "workspace_roots.ETA_DIAMOND_PROP_ALLOCATOR_PATH",
        "eta_engine/scripts/diamond_prop_drawdown_guard.py": "workspace_roots.ETA_PROP_HALT_FLAG_PATH",
        "eta_engine/scripts/diamond_prop_launch_readiness.py": "workspace_roots.ETA_DIAMOND_RETUNE_STATUS_PATH",
        "eta_engine/scripts/diamond_sizing_audit.py": "workspace_roots.ETA_DIAMOND_SIZING_AUDIT_PATH",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    launch_readiness = _read("eta_engine/scripts/diamond_prop_launch_readiness.py")
    assert "workspace_roots.ETA_DIAMOND_PROP_DRAWDOWN_GUARD_PATH" in launch_readiness
    assert "workspace_roots.ETA_DIAMOND_RETUNE_STATUS_PATH" in launch_readiness

    trade_close_targets = (
        "eta_engine/scripts/diamond_demotion_gate.py",
        "eta_engine/scripts/diamond_direction_stratify.py",
        "eta_engine/scripts/diamond_feed_sanity_audit.py",
        "eta_engine/scripts/diamond_promotion_gate.py",
        "eta_engine/scripts/diamond_prop_drawdown_guard.py",
        "eta_engine/scripts/diamond_sizing_audit.py",
    )
    for rel_path in trade_close_targets:
        text = _read(rel_path)
        assert 'WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"' not in text

    text = _read("eta_engine/scripts/diamond_prop_drawdown_guard.py")
    assert 'WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "prop_halt_active.flag"' not in text
    assert 'WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "prop_watch_active.flag"' not in text

    text = _read("eta_engine/scripts/diamond_prop_alert_dispatcher.py")
    assert 'WORKSPACE_ROOT / "logs" / "eta_engine" / "alerts_log.jsonl"' not in text
    assert 'WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "diamond_prop_alert_cursor.json"' not in text

    text = _read("eta_engine/scripts/diamond_ops_dashboard.py")
    assert (
        'WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "health" / '
        '"public_broker_close_truth_latest.json"'
    ) not in text


def test_secondary_diamond_path_cleanup_wave_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/scripts/diamond_authenticity_audit.py": "workspace_roots.ETA_DIAMOND_AUTHENTICITY_PATH",
        "eta_engine/scripts/diamond_live_paper_drift.py": "workspace_roots.ETA_DIAMOND_LIVE_PAPER_DRIFT_PATH",
        "eta_engine/scripts/diamond_preset_validator.py": "workspace_roots.ETA_DIAMOND_PRESET_VALIDATION_PATH",
        "eta_engine/scripts/diamond_prop_prelaunch_dryrun.py": "workspace_roots.ETA_DIAMOND_PROP_PRELAUNCH_DRYRUN_PATH",
        "eta_engine/scripts/diamond_qty_asymmetry_audit.py": "workspace_roots.ETA_DIAMOND_QTY_ASYMMETRY_PATH",
        "eta_engine/scripts/diamond_wave25_status.py": "workspace_roots.ETA_DIAMOND_WAVE25_STATUS_PATH",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    text = _read("eta_engine/scripts/diamond_qty_asymmetry_audit.py")
    assert 'WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"' not in text

    text = _read("eta_engine/scripts/diamond_wave25_status.py")
    assert 'return WORKSPACE_ROOT / "var" / "eta_engine" / "state"' not in text
    assert 'return _state_dir() / "health"' not in text

    text = _read("eta_engine/scripts/diamond_authenticity_audit.py")
    assert 'STATE_DIR = WORKSPACE_ROOT / "var" / "eta_engine" / "state"' not in text
    assert 'LOG_DIR = WORKSPACE_ROOT / "logs" / "eta_engine"' not in text


def test_tertiary_diamond_path_cleanup_wave_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/scripts/diamond_cpcv_runner.py": "workspace_roots.ETA_DIAMOND_CPCV_PATH",
        "eta_engine/scripts/diamond_data_sanitizer.py": "workspace_roots.ETA_DIAMOND_SANITIZER_PATH",
        "eta_engine/scripts/diamond_regime_stratify.py": "workspace_roots.ETA_DIAMOND_REGIME_STRATIFY_PATH",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    for rel_path in targets:
        text = _read(rel_path)
        assert 'WORKSPACE_ROOT / "var" / "eta_engine" / "state"' not in text
        assert 'ROOT / "state"' not in text

    cpcv = _read("eta_engine/scripts/diamond_cpcv_runner.py")
    assert "argparse.ArgumentParser(description=_console_help_description(__doc__))" in cpcv

    sanitizer = _read("eta_engine/scripts/diamond_data_sanitizer.py")
    assert "argparse.ArgumentParser(description=_console_help_description(__doc__))" in sanitizer

    regime = _read("eta_engine/scripts/diamond_regime_stratify.py")
    assert "argparse.ArgumentParser(description=_console_help_description(__doc__))" in regime


def test_sixth_audit_surface_cleanup_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/scripts/jarvis_ask.py": "workspace_roots.ETA_JARVIS_AUDIT_DIR",
        "eta_engine/scripts/run_anomaly_scan.py": "workspace_roots.ETA_ANOMALY_ALERT_STATE_PATH",
        "eta_engine/scripts/run_calibration_fit.py": "workspace_roots.ETA_CALIBRATION_MODEL_PATH",
        "eta_engine/scripts/score_policy_candidate.py": "workspace_roots.ETA_JARVIS_AUDIT_DIR",
        "eta_engine/obs/jarvis_verdict_webhook.py": "workspace_roots.ETA_VERDICT_WEBHOOK_CURSOR_PATH",
        "eta_engine/obs/jarvis_denial_rate_alerter.py": "workspace_roots.ETA_JARVIS_DENIAL_RATE_ALERT_STATE_PATH",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    jarvis_ask = _read("eta_engine/scripts/jarvis_ask.py")
    assert 'ROOT / "state" / "jarvis_audit"' not in jarvis_ask

    anomaly = _read("eta_engine/scripts/run_anomaly_scan.py")
    assert 'ROOT / "state" / "jarvis_audit"' not in anomaly
    assert 'ROOT / "state" / "anomaly" / "last_alert.json"' not in anomaly

    calibration = _read("eta_engine/scripts/run_calibration_fit.py")
    assert 'ROOT / "state" / "jarvis_audit"' not in calibration
    assert 'ROOT / "state" / "calibration" / "platt_sigmoid.json"' not in calibration

    score = _read("eta_engine/scripts/score_policy_candidate.py")
    assert 'ROOT / "state" / "jarvis_audit"' not in score

    webhook = _read("eta_engine/obs/jarvis_verdict_webhook.py")
    assert 'ROOT / "state" / "jarvis_audit"' not in webhook
    assert 'ROOT / "state" / "verdict_webhook" / "cursor.json"' not in webhook

    alerter = _read("eta_engine/obs/jarvis_denial_rate_alerter.py")
    assert 'ROOT / "var" / "alerter" / "jarvis_denial_rate_state.json"' not in alerter
    assert 'ROOT / "var" / "jarvis_audit"' not in alerter


def test_seventh_model_and_health_surface_cleanup_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/scripts/retrain_models.py": "workspace_roots.ETA_MODEL_ARTIFACT_DIR",
        "eta_engine/scripts/refresh_correlation_matrix.py": "workspace_roots.ETA_CORRELATION_ARTIFACT_DIR",
        "eta_engine/brain/jarvis_v3/health_check.py": "workspace_roots.ETA_MODEL_ARTIFACT_DIR",
        "eta_engine/brain/jarvis_v3/corr_regime_detector.py": "workspace_roots.ETA_CORRELATION_ARTIFACT_DIR",
        "eta_engine/scripts/backfill_trade_closes_data_source.py": "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH",
        "eta_engine/scripts/bot_pressure_test.py": "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH",
        "eta_engine/scripts/daily_loss_killswitch.py": "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH",
        "eta_engine/scripts/edge_analyzer.py": "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH",
        "eta_engine/scripts/elite_scoreboard.py": "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH",
        "eta_engine/scripts/monte_carlo_validator.py": "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH",
        "eta_engine/scripts/shadow_signal_logger.py": "workspace_roots.ETA_JARVIS_SHADOW_SIGNALS_PATH",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    retrain = _read("eta_engine/scripts/retrain_models.py")
    assert 'ROOT / "state" / "models"' not in retrain
    assert 'ROOT / "state" / "jarvis_audit"' not in retrain
    assert 'ROOT / "state" / "correlation"' not in retrain

    refresh = _read("eta_engine/scripts/refresh_correlation_matrix.py")
    assert 'ROOT / "state" / "correlation" / "learned.json"' not in refresh

    jarvis_health = _read("eta_engine/brain/jarvis_v3/health_check.py")
    assert 'ROOT / "state" / "models"' not in jarvis_health
    assert 'ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"' not in jarvis_health

    corr_regime = _read("eta_engine/brain/jarvis_v3/corr_regime_detector.py")
    assert 'ROOT / "state" / "correlation" / "learned.json"' not in corr_regime
    assert 'ROOT / "state" / "correlation_regime"' not in corr_regime

    trade_close_targets = (
        "eta_engine/scripts/backfill_trade_closes_data_source.py",
        "eta_engine/scripts/bot_pressure_test.py",
        "eta_engine/scripts/daily_loss_killswitch.py",
        "eta_engine/scripts/edge_analyzer.py",
        "eta_engine/scripts/elite_scoreboard.py",
        "eta_engine/scripts/monte_carlo_validator.py",
    )
    for rel_path in trade_close_targets:
        text = _read(rel_path)
        assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\trade_closes.jsonl" not in text

    shadow_signal_logger = _read("eta_engine/scripts/shadow_signal_logger.py")
    assert (
        r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\shadow_signals.jsonl"
    ) not in shadow_signal_logger


def test_eighth_dashboard_audit_surface_prefers_canonical_daily_logs() -> None:
    dashboard_api = _read("eta_engine/deploy/scripts/dashboard_api.py")

    assert "def _jarvis_audit_log_candidates" in dashboard_api
    assert "def _load_jarvis_audit_lines" in dashboard_api
    assert 'audit_path = _state_dir() / "jarvis_audit.jsonl"' not in dashboard_api
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\verdicts.jsonl")' not in dashboard_api


def test_ninth_jarvis_reader_surface_cleanup_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/brain/jarvis_v3/admin_query.py": "workspace_roots.ETA_JARVIS_VERDICTS_PATH",
        "eta_engine/brain/jarvis_v3/daily_brief.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/divergence_detector.py": "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH",
        "eta_engine/brain/jarvis_v3/postmortem.py": "workspace_roots.ETA_JARVIS_POSTMORTEM_DIR",
        "eta_engine/brain/jarvis_v3/replay_engine.py": "workspace_roots.ETA_JARVIS_VERDICTS_PATH",
        "eta_engine/brain/jarvis_v3/risk_budget_allocator.py": "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    admin_query = _read("eta_engine/brain/jarvis_v3/admin_query.py")
    assert 'ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"' not in admin_query
    assert 'ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl"' not in admin_query

    daily_brief = _read("eta_engine/brain/jarvis_v3/daily_brief.py")
    assert 'DEFAULT_STATE_DIR = ROOT / "state" / "jarvis_intel"' not in daily_brief
    assert 'DEFAULT_BRIEF_DIR = DEFAULT_STATE_DIR / "daily_briefs"' not in daily_brief

    divergence = _read("eta_engine/brain/jarvis_v3/divergence_detector.py")
    assert 'ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl"' not in divergence

    postmortem = _read("eta_engine/brain/jarvis_v3/postmortem.py")
    assert 'ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"' not in postmortem
    assert 'ROOT / "state" / "jarvis_intel" / "postmortems"' not in postmortem

    replay = _read("eta_engine/brain/jarvis_v3/replay_engine.py")
    assert 'ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"' not in replay
    assert 'ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl"' not in replay

    risk_budget = _read("eta_engine/brain/jarvis_v3/risk_budget_allocator.py")
    assert 'ROOT / "state" / "jarvis_intel" / "trade_closes.jsonl"' not in risk_budget


def test_tenth_jarvis_intel_registry_surface_cleanup_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/brain/jarvis_v3/ab_framework.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/intelligence.py": "workspace_roots.ETA_JARVIS_VERDICTS_PATH",
        "eta_engine/brain/jarvis_v3/operator_coach.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/override_retrospective.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/pre_live_gate.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/regression_test_set.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/self_drift_monitor.py": "workspace_roots.ETA_JARVIS_VERDICTS_PATH",
        "eta_engine/brain/jarvis_v3/shadow_orchestrator.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/shadow_pipeline.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/skill_health_registry.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
        "eta_engine/brain/jarvis_v3/thesis_tracker.py": "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    raw_state_targets = (
        "eta_engine/brain/jarvis_v3/ab_framework.py",
        "eta_engine/brain/jarvis_v3/operator_coach.py",
        "eta_engine/brain/jarvis_v3/override_retrospective.py",
        "eta_engine/brain/jarvis_v3/pre_live_gate.py",
        "eta_engine/brain/jarvis_v3/regression_test_set.py",
        "eta_engine/brain/jarvis_v3/shadow_orchestrator.py",
        "eta_engine/brain/jarvis_v3/shadow_pipeline.py",
        "eta_engine/brain/jarvis_v3/skill_health_registry.py",
        "eta_engine/brain/jarvis_v3/thesis_tracker.py",
    )
    for rel_path in raw_state_targets:
        text = _read(rel_path)
        assert 'ROOT / "state" / "jarvis_intel"' not in text

    intelligence = _read("eta_engine/brain/jarvis_v3/intelligence.py")
    assert 'ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"' not in intelligence

    self_drift = _read("eta_engine/brain/jarvis_v3/self_drift_monitor.py")
    assert 'ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"' not in self_drift

    shadow_pipeline = _read("eta_engine/brain/jarvis_v3/shadow_pipeline.py")
    assert r'ROOT / "var" / "eta_engine" / "state" / "shadow_fills.jsonl"' not in shadow_pipeline


def test_eleventh_runtime_state_surface_cleanup_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/brain/jarvis_v3/calibration.py": "workspace_roots.ETA_CALIBRATOR_LABELS_PATH",
        "eta_engine/brain/jarvis_v3/hot_learner.py": "workspace_roots.ETA_HOT_LEARNER_STATE_PATH",
        "eta_engine/brain/jarvis_v3/portfolio_brain.py": "workspace_roots.ETA_FLEET_STATE_PATH",
        "eta_engine/brain/jarvis_v3/trace_emitter.py": "workspace_roots.ETA_JARVIS_TRACE_PATH",
        "eta_engine/brain/jarvis_v3/zeus.py": "workspace_roots.ETA_KAIZEN_LATEST_PATH",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    calibration = _read("eta_engine/brain/jarvis_v3/calibration.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\calibrator_labels.jsonl")' not in calibration

    hot_learner = _read("eta_engine/brain/jarvis_v3/hot_learner.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hot_learner.json")' not in hot_learner

    portfolio_brain = _read("eta_engine/brain/jarvis_v3/portfolio_brain.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\fleet_state.json")' not in portfolio_brain

    trace_emitter = _read("eta_engine/brain/jarvis_v3/trace_emitter.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_trace.jsonl")' not in trace_emitter

    zeus = _read("eta_engine/brain/jarvis_v3/zeus.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_latest.json")' not in zeus


def test_twelfth_ops_runtime_surface_cleanup_uses_workspace_root_helpers() -> None:
    targets = {
        "eta_engine/scripts/eta_alert_dispatcher.py": "workspace_roots.ETA_ETA_EVENTS_LOG_PATH",
        "eta_engine/scripts/hermes_dispatcher.py": "workspace_roots.ETA_JARVIS_V3_EVENTS_PATH",
        "eta_engine/scripts/tws_watchdog.py": "workspace_roots.ETA_TWS_WATCHDOG_STATUS_PATH",
        "eta_engine/deploy/scripts/dashboard_api.py": "workspace_roots.ETA_DATA_INVENTORY_SNAPSHOT_PATH",
    }
    for rel_path, token in targets.items():
        text = _read(rel_path)
        assert "workspace_roots" in text
        assert token in text

    eta_alert_dispatcher = _read("eta_engine/scripts/eta_alert_dispatcher.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state")' not in eta_alert_dispatcher

    hermes_dispatcher = _read("eta_engine/scripts/hermes_dispatcher.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_v3_events.jsonl")' not in hermes_dispatcher

    tws_watchdog = _read("eta_engine/scripts/tws_watchdog.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\tws_watchdog.json")' not in tws_watchdog

    dashboard_api = _read("eta_engine/deploy/scripts/dashboard_api.py")
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\data_inventory.json")' not in dashboard_api


def test_cloudflare_named_setup_writes_logs_and_state_under_workspace() -> None:
    text = _read("eta_engine/deploy/scripts/cloudflare_setup_named.ps1")
    assert r"LOCALAPPDATA\eta_engine" not in text
    assert 'Join-Path $workspaceRoot "logs"' in text
    assert 'Join-Path $workspaceRoot "var\\cloudflare"' in text


def test_windows_deploy_defaults_drop_legacy_install_and_localappdata_paths() -> None:
    targets = (
        "eta_engine/deploy/install_windows.ps1",
        "eta_engine/deploy/bin/eta.cmd",
        "eta_engine/deploy/scripts/optimize_vps.ps1",
        "eta_engine/deploy/scripts/register_fleet_tasks.ps1",
        "eta_engine/deploy/scripts/register_operator_tasks.ps1",
        "eta_engine/deploy/scripts/register_tasks.ps1",
        "eta_engine/deploy/scripts/set_vps_env_vars.ps1",
        "eta_engine/deploy/scripts/supercharge_vps.ps1",
        "eta_engine/deploy/scripts/vps_supercharge_bootstrap.ps1",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert r"C:\eta_engine" not in text
        assert r"LOCALAPPDATA\eta_engine" not in text


def test_runtime_helpers_drop_localappdata_eta_state_paths() -> None:
    targets = (
        "eta_engine/scripts/alerts_log_smoke.py",
        "eta_engine/scripts/dashboard_proxy_watchdog.py",
        "eta_engine/scripts/drift_watchdog_smoke.py",
        "eta_engine/scripts/ibc_cutover_readiness.py",
        "eta_engine/scripts/operator_queue_heartbeat.py",
        "eta_engine/scripts/operator_queue_snapshot.py",
        "eta_engine/scripts/runtime_log_smoke.py",
        "eta_engine/scripts/vps_failover_summary.py",
        "eta_engine/deploy/scripts/live_codex_smoke.py",
        "eta_engine/deploy/scripts/live_claude_smoke.py",
        "eta_engine/deploy/scripts/avengers_daemon.py",
        "eta_engine/deploy/scripts/register_dashboard_proxy_watchdog_task.ps1",
        "eta_engine/deploy/scripts/register_operator_queue_heartbeat_task.ps1",
        "eta_engine/deploy/scripts/register_cloudflare_quick.ps1",
        "eta_engine/deploy/scripts/run_operator_queue_heartbeat_task.cmd",
        "eta_engine/deploy/scripts/run_dashboard_8421.ps1",
        "eta_engine/deploy/uninstall_windows.ps1",
        "eta_engine/obs/daemon_recovery_watchdog.py",
        "eta_engine/obs/heartbeat_writer.py",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert "LOCALAPPDATA" not in text
        assert r"AppData\Local\eta_engine" not in text

    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read("eta_engine/deploy/scripts/live_codex_smoke.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read("eta_engine/deploy/scripts/live_claude_smoke.py")
    assert "ETA_RUNTIME_ALERTS_LOG_PATH" in _read("eta_engine/scripts/alerts_log_smoke.py")
    assert "ETA_DRIFT_WATCHDOG_LOG_PATH" in _read("eta_engine/scripts/drift_watchdog_smoke.py")
    assert "ETA_RUNTIME_LOG_PATH" in _read("eta_engine/scripts/runtime_log_smoke.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read("eta_engine/deploy/scripts/avengers_daemon.py")
    assert "workspace_roots.ETA_RUNTIME_LOG_DIR" in _read("eta_engine/deploy/scripts/avengers_daemon.py")
    assert 'Path.home() / ".local" / "state" / "eta_engine"' not in _read(
        "eta_engine/deploy/scripts/avengers_daemon.py"
    )
    assert 'Path.home() / ".local" / "log" / "eta_engine"' not in _read("eta_engine/deploy/scripts/avengers_daemon.py")
    assert "vps_failover_drill.collect_checks" in _read("eta_engine/scripts/vps_failover_summary.py")
    run_research_grid = _read("eta_engine/scripts/run_research_grid.py")
    assert "workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR" in run_research_grid
    assert 'workspace_roots.CRYPTO_HISTORY_ROOT / "BTCFUND_8h.csv"' in run_research_grid
    assert 'workspace_roots.MNQ_DATA_ROOT / "BTCFUND_8h.csv"' in run_research_grid
    assert 'workspace_roots.CRYPTO_HISTORY_ROOT / "btc_funding_8h.csv"' in run_research_grid
    assert r'Path(r"C:\EvolutionaryTradingAlgo\data\crypto\history\BTCFUND_8h.csv")' not in run_research_grid
    assert r'Path(r"C:\EvolutionaryTradingAlgo\mnq_data\BTCFUND_8h.csv")' not in run_research_grid

    feed_research_grid = _read("eta_engine/feeds/run_research_grid.py")
    assert 'workspace_roots.CRYPTO_HISTORY_ROOT / "BTCFUND_8h.csv"' in feed_research_grid
    assert 'workspace_roots.MNQ_DATA_ROOT / "BTCFUND_8h.csv"' in feed_research_grid
    assert 'workspace_roots.CRYPTO_HISTORY_ROOT / "btc_funding_8h.csv"' in feed_research_grid
    assert r'Path(r"C:\EvolutionaryTradingAlgo\data\crypto\history\BTCFUND_8h.csv")' not in feed_research_grid
    assert r'Path(r"C:\EvolutionaryTradingAlgo\mnq_data\BTCFUND_8h.csv")' not in feed_research_grid
    assert "ETA_LIVE_DATA_RUNTIME_DIR" in _read("eta_engine/scripts/dual_data_collector.py")
    assert 'ROOT / "docs" / "live_data"' not in _read("eta_engine/scripts/dual_data_collector.py")
    assert "ETA_RUNTIME_DECISION_JOURNAL_PATH" in _read("eta_engine/scripts/announce_data_library.py")
    assert "ETA_RUNTIME_DECISION_JOURNAL_PATH" in _read("eta_engine/scripts/drift_check.py")
    assert "ETA_RUNTIME_DECISION_JOURNAL_PATH" in _read("eta_engine/scripts/monte_carlo_stress.py")
    assert "eta_engine\\docs\\decision_journal.jsonl" not in _read("eta_engine/scripts/runtime_readiness_check.ps1")
    assert "firm_command_center\\var\\reports\\decision_journal.jsonl" not in _read(
        "eta_engine/scripts/runtime_readiness_check.ps1"
    )
    assert "ETA_RUNTIME_DECISION_JOURNAL_PATH" in _read("eta_engine/brain/jarvis_v3/health_check.py")
    assert "workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH" in _read("eta_engine/scripts/operator_queue_snapshot.py")
    assert "workspace_roots.ETA_OPERATOR_QUEUE_PREVIOUS_SNAPSHOT_PATH" in _read(
        "eta_engine/scripts/operator_queue_snapshot.py"
    )
    assert "workspace_roots.ETA_IBC_CUTOVER_READINESS_PATH" in _read("eta_engine/scripts/ibc_cutover_readiness.py")
    assert "workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH" in _read("eta_engine/scripts/operator_queue_heartbeat.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read("eta_engine/obs/heartbeat_writer.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read("eta_engine/obs/daemon_recovery_watchdog.py")
    assert 'ETA_ENGINE_ROOT / "state"' not in _read("eta_engine/obs/daemon_recovery_watchdog.py")
    assert "workspace_roots.ETA_JARVIS_DRIFT_JOURNAL_PATH" in _read("eta_engine/brain/avengers/drift_detector.py")
    assert "workspace_roots.ETA_SHARED_BREAKER_STATE_PATH" in _read("eta_engine/brain/avengers/shared_breaker.py")
    assert "workspace_roots.ETA_DEADMAN_SENTINEL_PATH" in _read("eta_engine/brain/avengers/deadman.py")
    assert "workspace_roots.ETA_PROMOTION_STATE_PATH" in _read("eta_engine/brain/avengers/promotion.py")
    assert "workspace_roots.ETA_AVENGERS_JOURNAL_PATH" in _read("eta_engine/brain/avengers/base.py")
    assert "workspace_roots.ETA_CALIBRATION_JOURNAL_PATH" in _read("eta_engine/brain/avengers/calibration_loop.py")
    assert "calibration_journal_read_path" in _read("eta_engine/brain/avengers/calibration_loop.py")
    assert "avengers_journal_read_path" in _read("eta_engine/brain/avengers/precedent_cache.py")
    assert "avengers_journal_read_path" in _read("eta_engine/brain/avengers/cost_forecast.py")
    assert "avengers_journal_read_path" in _read("eta_engine/brain/avengers/watchdog.py")
    assert "workspace_roots.ETA_RUNTIME_ALERTS_LOG_PATH" in _read("eta_engine/brain/avengers/push.py")
    assert 'Path.home() / ".jarvis" / "alerts.jsonl"' not in _read("eta_engine/brain/avengers/push.py")
    assert "workspace_roots.ETA_AVENGER_DAEMON_PID_DIR" in _read("eta_engine/brain/avengers/daemon.py")
    assert "workspace_roots.ETA_AVENGER_METRICS_PATH" in _read("eta_engine/brain/avengers/daemon.py")
    assert 'Path.home() / ".jarvis"' not in _read("eta_engine/brain/avengers/daemon.py")
    assert "~/.jarvis/metrics.prom" not in _read("eta_engine/brain/avengers/daemon.py")
    assert "$env:ETA_STATE_DIR = $stateDir" in _read("eta_engine/deploy/scripts/run_dashboard_8421.ps1")


def test_legacy_docs_decision_journal_is_ignored_runtime_state() -> None:
    gitignore = _read("eta_engine/.gitignore")

    assert "docs/decision_journal.jsonl" in gitignore
    assert "docs/alerts_log.jsonl" in gitignore
    assert "docs/runtime_log.jsonl" in gitignore
    assert "docs/drift_watchdog.jsonl" in gitignore
    assert "docs/live_data/*.jsonl" in gitignore
    assert "docs/live_data/collector_last_run.json" in gitignore


def test_smoke_check_uses_workspace_state_and_log_dirs() -> None:
    text = _read("eta_engine/deploy/scripts/smoke_check.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in text
    assert "workspace_roots.ETA_RUNTIME_LOG_DIR" in text
    assert ".local" not in text


def test_deploy_runbooks_use_workspace_state_and_log_dirs() -> None:
    targets = (
        "eta_engine/deploy/README.md",
        "eta_engine/deploy/HOST_RUNBOOK.md",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert "~/.local/state/eta_engine" not in text
        assert "~/.local/log/eta_engine" not in text
        assert "var/eta_engine/state" in text
        assert "logs/eta_engine" in text


def test_tradingview_runtime_defaults_use_workspace_paths() -> None:
    targets = (
        "eta_engine/data/tradingview/auth.py",
        "eta_engine/data/tradingview/journal.py",
        "eta_engine/data/tradingview/__init__.py",
        "eta_engine/scripts/run_tradingview_capture.py",
        "eta_engine/scripts/tradingview_auth_refresh.py",
        "eta_engine/deploy/systemd/eta-tradingview-capture.service",
        "eta_engine/deploy/configs/process-compose.yaml",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert "~/.local/state/eta_engine" not in text
        assert "${HOME}/.local/state/eta_engine" not in text
        assert "%h/.local/state/eta_engine" not in text
        assert "~/eta_data/tradingview" not in text
        assert "%h/eta_data/tradingview" not in text

    assert "workspace_roots.ETA_TRADINGVIEW_AUTH_STATE_PATH" in _read("eta_engine/data/tradingview/auth.py")
    assert "workspace_roots.ETA_TRADINGVIEW_DATA_ROOT" in _read("eta_engine/data/tradingview/journal.py")
    assert "../var/eta_engine/state/tradingview_auth.json" in _read(
        "eta_engine/deploy/systemd/eta-tradingview-capture.service"
    )
    assert "../var/eta_engine/state/live_data/tradingview" in _read(
        "eta_engine/deploy/systemd/eta-tradingview-capture.service"
    )


def test_systemd_install_defaults_use_workspace_state_and_log_paths() -> None:
    unit_targets = (
        "eta_engine/deploy/systemd/jarvis-live.service",
        "eta_engine/deploy/systemd/avengers-fleet.service",
        "eta_engine/deploy/systemd/eta-dashboard.service",
    )
    for rel_path in unit_targets:
        text = _read(rel_path)
        assert "%h/.local/state/eta_engine" not in text
        assert "%h/.local/log/eta_engine" not in text
        assert "__INSTALL_DIR__/../var/eta_engine/state" in text
        assert "__INSTALL_DIR__/../logs/eta_engine" in text

    installer = _read("eta_engine/deploy/install_vps.sh")
    assert "$HOME/.local/state/eta_engine" not in installer
    assert "$HOME/.local/log/eta_engine" not in installer
    assert "$INSTALL_DIR/../var/eta_engine/state" in installer
    assert "$INSTALL_DIR/../logs/eta_engine" in installer


def test_doc_cleanup_wave_drops_legacy_paths() -> None:
    targets = (
        "eta_engine/docs/research_log/2026-04-26_post_rebrand_baseline.md",
        "eta_engine/docs/research_log/2026-04-26_supercharge.md",
        "eta_engine/docs/research_log/paid_data_aggregator_landscape_20260427.md",
        "eta_engine/docs/research_log/supercharge_full_stack_findings_20260427.md",
        "eta_engine/docs/superpowers/plans/2026-04-28-cursor-dashboard-cutover.md",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert r"C:\mnq_data" not in text
        assert "C:/mnq_data" not in text
        assert r"C:\crypto_data" not in text
        assert "C:/crypto_data" not in text
        assert r"LOCALAPPDATA\eta_engine" not in text


def test_weekly_review_current_surfaces_drop_legacy_workspace_paths() -> None:
    targets = (
        "eta_engine/docs/weekly_review_latest.json",
        "eta_engine/docs/weekly_review_latest.txt",
        "eta_engine/docs/weekly_review_log.json",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert r"C:\Users\edwar\OneDrive" not in text
        assert r"OneDrive\Desktop\Base" not in text


def test_workspace_roots_helper_docstring_avoids_legacy_external_paths() -> None:
    text = _read("eta_engine/scripts/workspace_roots.py")
    assert r"C:\mnq_data" not in text
    assert r"C:\crypto_data" not in text
    assert r"LOCALAPPDATA\eta_engine" not in text


def test_regime_gated_default_entry_path_passes_regime_provider() -> None:
    text = _read("eta_engine/scripts/run_btc_regime_gated_walk_forward.py")
    # Anchor with the open-paren so we don't false-match the suffix of
    # ``regime_provider,\n        args.etf_path`` (which legitimately
    # passes regime_provider on its own line just before etf_path).
    assert "(\n        provider,\n        args.etf_path" not in text
    # The factory must receive provider, regime_provider, etf_path
    # (in that order â€” multi-line call).
    assert "provider,\n        regime_provider,\n        args.etf_path" in text


def test_b_class_state_writers_use_canonical_var_state_path() -> None:
    """B-class state-file writers (LEGACY_PATH_AUDIT.md) write canonical.

    After the 2026-05-04 migration, the B1â€“B5 writers must default to
    ``var/eta_engine/state`` and only consult the legacy in-repo
    ``eta_engine/state`` path as a read fallback. The string checks
    below pin both halves of that contract.
    """
    # B1: dashboard_api.py default state dir is now canonical, with the
    # legacy in-repo path kept only as a labelled fallback.
    dashboard_api = _read("eta_engine/deploy/scripts/dashboard_api.py")
    assert '_DEFAULT_STATE = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"' in dashboard_api
    # ruff/black collapsed the column-alignment to single space â€” keep the
    # contract on the assignment shape rather than the visual column.
    assert '_LEGACY_STATE = _REPO_ROOT / "state"' in dashboard_api
    assert '_DEFAULT_LOG = _WORKSPACE_ROOT / "logs" / "eta_engine"' in dashboard_api
    # The AppData-Local fallback was a separate hard-rule violation;
    # ensure the policy_diff endpoint no longer falls back to it.
    assert "AppData/Local/eta_engine" not in dashboard_api

    # B2: run_eval.py uses workspace_roots constants for the canonical
    # promptfoo output path with a legacy alias for the read fallback.
    run_eval = _read("eta_engine/eval/run_eval.py")
    assert "workspace_roots.ETA_EVAL_PROMPTFOO_RESULTS_PATH" in run_eval
    assert "workspace_roots.ETA_LEGACY_EVAL_PROMPTFOO_RESULTS_PATH" in run_eval

    # B3: hermes_bridge `/kill confirm` writes to a single canonical
    # latch path (collapsed from the previous three-target fan-out).
    hermes = _read("eta_engine/brain/jarvis_v3/hermes_bridge.py")
    assert "workspace_roots.ETA_HERMES_KILL_LATCH_PATH" in hermes
    assert "workspace_roots.ETA_HERMES_STATE_PATH" in hermes
    assert "workspace_roots.ETA_JARVIS_INTEL_STATE_DIR" in hermes
    assert "workspace_roots.ETA_JARVIS_LIVE_HEALTH_PATH" in hermes
    # The triple fan-out is gone â€” the `latch_paths = [...]` literal
    # that listed three destinations should no longer appear.
    assert "latch_paths = [" not in hermes
    assert 'state_dir = ROOT / "state" / "jarvis_intel"' not in hermes
    assert 'ROOT / "docs" / "jarvis_live_health.json"' not in hermes

    # B4: read-only verdict inspection scripts use the canonical path
    # with legacy fallback.
    cond_check = _read("eta_engine/deploy/scripts/cond_check.py")
    quick_check = _read("eta_engine/deploy/scripts/quick_check.py")
    recent_verdicts = _read("eta_engine/deploy/scripts/recent_verdicts.py")
    for text in (cond_check, quick_check, recent_verdicts):
        assert "workspace_roots.ETA_JARVIS_VERDICTS_PATH" in text
        assert "workspace_roots.ETA_LEGACY_JARVIS_VERDICTS_PATH" in text
        # The hard-coded legacy literal must be gone in all three.
        assert "C:/EvolutionaryTradingAlgo/eta_engine/state/jarvis_intel" not in text

    ceiling_audit = _read("eta_engine/deploy/scripts/ceiling_audit.py")
    assert "workspace_roots.WORKSPACE_ROOT" in ceiling_audit
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in ceiling_audit
    assert "workspace_roots.ETA_JARVIS_VERDICTS_PATH" in ceiling_audit
    assert 'verdict_path = ENGINE_ROOT / "state" / "jarvis_intel" / "verdicts.jsonl"' not in ceiling_audit


def test_b_class_kill_switch_latch_default_resolves_to_canonical_workspace() -> None:
    """run_eta_live default latch path lands under workspace var/."""
    scripts_text = _read("eta_engine/scripts/run_eta_live.py")
    feeds_text = _read("eta_engine/feeds/run_eta_live.py")
    # The canonical implementation now lives only in scripts/; feeds/run_eta_live.py
    # is an intentional compatibility shim to avoid a second drifting copy.
    assert ('WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "kill_switch_latch.json"') in scripts_text
    assert 'ROOT / "state" / "kill_switch_latch.json"' not in scripts_text
    assert "default_legacy_path()" in scripts_text
    assert "eta_engine.scripts.run_eta_live" in feeds_text
    assert "Compatibility shim" in feeds_text


def test_b_class_helper_modules_expose_canonical_default_resolvers() -> None:
    """KillSwitchLatch and TrailingDDTracker expose canonical helpers."""
    latch_text = _read("eta_engine/core/kill_switch_latch.py")
    tracker_text = _read("eta_engine/core/trailing_dd_tracker.py")
    for text in (latch_text, tracker_text):
        assert "def default_path()" in text
        assert "def default_legacy_path()" in text
        assert "def resolve_existing_path()" in text
    assert "ETA_KILL_SWITCH_LATCH_PATH" in latch_text
    assert "ETA_TRAILING_DD_TRACKER_PATH" in tracker_text


def test_b_class_fm_health_writer_uses_canonical_workspace_path() -> None:
    """force_multiplier_health.py + install_fm_health_task.ps1 write canonical.

    The probe script (producer when --json-out is set) and the Task
    Scheduler installer (caller) both default to the canonical
    ``var/eta_engine/state/fm_health.json`` path. The producer also
    exposes the standard helper trio used by the other B-class
    state writers.
    """
    probe_text = _read("eta_engine/scripts/force_multiplier_health.py")
    installer_text = _read("eta_engine/scripts/install_fm_health_task.ps1")

    # Producer exposes the helper trio + uses the workspace_roots constant.
    assert "def default_path()" in probe_text
    assert "def default_legacy_path()" in probe_text
    assert "def resolve_existing_path()" in probe_text
    assert "workspace_roots.ETA_FM_HEALTH_SNAPSHOT_PATH" in probe_text
    assert '_PATH_ENV_VAR: str = "ETA_FM_HEALTH_SNAPSHOT_PATH"' in probe_text

    # Help text and installer point at the canonical var/ path.
    assert "var/eta_engine/state/fm_health.json" in probe_text
    assert "var\\eta_engine\\state\\fm_health.json" in installer_text
    assert "[string]$TaskName = 'ETA-FM-HealthProbe'" in installer_text
    assert "[int]$IntervalMinutes = 15" in installer_text
    assert "eta_engine\\.venv\\Scripts\\python.exe" in installer_text
    assert "-RepetitionInterval $interval" in installer_text
    assert "RepetitionDuration (New-TimeSpan -Days 3650)" in installer_text
    assert "[TimeSpan]::MaxValue" not in installer_text
    # The legacy in-repo path is gone from the installer's write target.
    assert "'eta_engine\\state\\fm_health.json'" not in installer_text


def test_supervisor_and_wiring_audit_use_workspace_root_helpers() -> None:
    wiring_audit = _read("eta_engine/scripts/jarvis_wiring_audit.py")
    assert "workspace_roots.ETA_ENGINE_ROOT / \"brain\" / \"jarvis_v3\"" in wiring_audit
    assert "workspace_roots.ETA_JARVIS_TRACE_PATH" in wiring_audit
    assert "workspace_roots.ETA_JARVIS_WIRING_AUDIT_PATH" in wiring_audit
    assert 'REPO_ROOT / "eta_engine" / "brain" / "jarvis_v3"' not in wiring_audit
    assert 'REPO_ROOT / "var" / "eta_engine" / "state" / "jarvis_trace.jsonl"' not in wiring_audit
    assert 'REPO_ROOT / "var" / "eta_engine" / "state" / "jarvis_wiring_audit.json"' not in wiring_audit

    supervisor = _read("eta_engine/scripts/jarvis_strategy_supervisor.py")
    assert "workspace_roots.ETA_FLEET_STATE_PATH" in supervisor
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\fleet_state.json")' not in supervisor


def test_soak_and_allocation_surfaces_use_workspace_root_helpers() -> None:
    soak_status_api = _read("eta_engine/deploy/status_page/soak_status_api.py")
    assert "workspace_roots.ETA_PAPER_SOAK_LEDGER_PATH" in soak_status_api
    assert 'workspace_roots.ETA_ENGINE_ROOT / "strategies" / "per_bot_registry.py"' in soak_status_api
    assert r'sys.path.insert(0, r"C:\EvolutionaryTradingAlgo")' not in soak_status_api
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\paper_soak_ledger.json")' not in soak_status_api

    capital_allocator = _read("eta_engine/feeds/capital_allocator.py")
    assert "workspace_roots.ETA_CAPITAL_ALLOCATION_PATH" in capital_allocator
    assert "workspace_roots.ETA_DIAMOND_LEADERBOARD_PATH" in capital_allocator
    assert "workspace_roots.ETA_PROP_HALT_FLAG_PATH" in capital_allocator
    assert "workspace_roots.ETA_PROP_WATCH_FLAG_PATH" in capital_allocator
    assert "workspace_roots.ETA_DIAMOND_PROP_DRAWDOWN_GUARD_PATH" in capital_allocator
    assert "workspace_roots.ETA_DIAMOND_PROP_LAUNCH_READINESS_PATH" in capital_allocator
    assert "workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH" in capital_allocator
    assert "workspace_roots.ETA_BOT_LIFECYCLE_STATE_PATH" in capital_allocator
    assert "workspace_roots.ETA_PAPER_SOAK_LEDGER_PATH" in capital_allocator
    assert 'workspace_roots.ETA_ENGINE_ROOT / "strategies" / "per_bot_registry.py"' in capital_allocator
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\capital_allocation.json")' not in capital_allocator
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\paper_soak_ledger.json")' not in capital_allocator
    assert r'Path(r"C:\EvolutionaryTradingAlgo\eta_engine\strategies\per_bot_registry.py")' not in capital_allocator

    soak_direct = _read("eta_engine/scripts/soak_direct.py")
    assert "workspace_roots.ETA_PAPER_SOAK_LEDGER_PATH" in soak_direct
    assert 'workspace_roots.ETA_RUNTIME_LOG_DIR / "soak_direct.log"' in soak_direct
    assert r'Path(r"C:\EvolutionaryTradingAlgo\firm_command_center\var\soak_direct.log")' not in soak_direct
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\paper_soak_ledger.json")' not in soak_direct


def test_policy_and_sidecar_state_surfaces_use_workspace_root_helpers() -> None:
    v23 = _read("eta_engine/brain/jarvis_v3/policies/v23_fleet_aware.py")
    assert "workspace_roots.ETA_REGIME_STATE_PATH" in v23
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\regime_state.json")' not in v23

    v25 = _read("eta_engine/brain/jarvis_v3/policies/v25_class_loss_limit.py")
    assert "workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH" in v25
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json")' not in v25

    v26 = _read("eta_engine/brain/jarvis_v3/policies/v26_fill_confirmation.py")
    assert "workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH" in v26
    assert "workspace_roots.ETA_BROKER_ROUTER_FILLS_PATH" in v26
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json")' not in v26
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\broker_router_fills.jsonl")' not in v26

    v27 = _read("eta_engine/brain/jarvis_v3/policies/v27_sharpe_drift.py")
    assert "workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH" in v27
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_intel\supervisor\heartbeat.json")' not in v27

    events = _read("eta_engine/brain/jarvis_v3/policies/_v3_events.py")
    assert "workspace_roots.ETA_JARVIS_V3_EVENTS_PATH" in events
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\jarvis_v3_events.jsonl")' not in events

    agent_registry = _read("eta_engine/brain/jarvis_v3/agent_registry.py")
    assert "workspace_roots.ETA_AGENT_REGISTRY_PATH" in agent_registry
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\agent_registry.json")' not in agent_registry

    hermes_overrides = _read("eta_engine/brain/jarvis_v3/hermes_overrides.py")
    assert "workspace_roots.ETA_HERMES_OVERRIDES_PATH" in hermes_overrides
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_overrides.json")' not in hermes_overrides

    sentiment_overlay = _read("eta_engine/brain/jarvis_v3/sentiment_overlay.py")
    assert "workspace_roots.ETA_SENTIMENT_CACHE_DIR" in sentiment_overlay
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\sentiment")' not in sentiment_overlay

    trade_narrator = _read("eta_engine/brain/jarvis_v3/trade_narrator.py")
    assert "workspace_roots.ETA_TRADE_JOURNAL_DIR" in trade_narrator
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\trade_journal")' not in trade_narrator


def test_jarvis_live_and_fleet_sweep_use_workspace_root_helpers() -> None:
    jarvis_live = _read("eta_engine/scripts/jarvis_live.py")
    assert "workspace_roots.default_premarket_inputs_path()" in jarvis_live
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in jarvis_live
    assert "workspace_roots.ETA_JARVIS_LIVE_HEALTH_PATH" in jarvis_live
    assert '"ETA_STATE_DIR", "C:/EvolutionaryTradingAlgo/var/eta_engine/state"' not in jarvis_live
    assert 'ROOT / "docs" / "jarvis_live_health.json"' not in jarvis_live
    assert 'DEFAULT_INPUTS = ROOT / "docs" / "premarket_inputs.json"' not in jarvis_live
    assert 'DEFAULT_OUT_DIR = ROOT / "docs"' not in jarvis_live

    btc_broker_fleet = _read("eta_engine/scripts/btc_broker_fleet.py")
    assert "workspace_roots.ETA_BTC_BROKER_FLEET_STATE_DIR" in btc_broker_fleet
    assert 'ROOT / "docs" / "btc_live" / "broker_fleet"' not in btc_broker_fleet

    btc_live = _read("eta_engine/scripts/btc_live.py")
    assert "workspace_roots.ETA_BTC_PAPER_RUN_LATEST_PATH" in btc_live
    assert "workspace_roots.ETA_BTC_LIVE_STATE_DIR" in btc_live
    assert 'ROOT / "docs" / "btc_paper" / "btc_paper_run_latest.json"' not in btc_live
    assert 'ROOT / "docs" / "btc_live"' not in btc_live

    btc_paper_trade = _read("eta_engine/scripts/btc_paper_trade.py")
    assert "workspace_roots.ETA_BTC_PAPER_STATE_DIR" in btc_paper_trade
    assert 'ROOT / "docs" / "btc_paper"' not in btc_paper_trade

    btc_paper_lane = _read("eta_engine/scripts/btc_paper_lane.py")
    assert "var/eta_engine/state/broker_fleet/btc_paper_trades.jsonl" in btc_paper_lane
    assert "docs/btc_live/broker_fleet/btc_paper_trades.jsonl" not in btc_paper_lane

    trade_journal_reconcile = _read("eta_engine/scripts/_trade_journal_reconcile.py")
    assert "workspace_roots.ETA_BTC_LIVE_DECISIONS_PATH" in trade_journal_reconcile
    assert "workspace_roots.default_btc_live_decisions_path()" in trade_journal_reconcile
    assert 'ROOT / "docs" / "btc_live" / "btc_live_decisions.jsonl"' not in trade_journal_reconcile

    trade_journal_reconcile_feed = _read("eta_engine/feeds/_trade_journal_reconcile.py")
    assert "Compatibility shim" in trade_journal_reconcile_feed
    assert "build_script_shim" in trade_journal_reconcile_feed
    assert "eta_engine.scripts._trade_journal_reconcile" in trade_journal_reconcile_feed
    assert 'ROOT / "docs" / "btc_live" / "btc_live_decisions.jsonl"' not in trade_journal_reconcile_feed

    broker_equity_drift_runbook = _read("eta_engine/docs/runbooks/broker_equity_drift_response.md")
    assert "var/eta_engine/state/btc_paper/btc_paper_journal.jsonl" in broker_equity_drift_runbook
    assert "docs/btc_live/btc_paper_journal.jsonl" not in broker_equity_drift_runbook
    assert "canonical broker connection/auth probe" in broker_equity_drift_runbook
    assert "Restart the\n  broker session:" not in broker_equity_drift_runbook
    assert "If the follow-up probe still fails" in broker_equity_drift_runbook

    jarvis_supervised_paper_run = _read("eta_engine/docs/runbooks/jarvis_supervised_paper_run.md")
    assert "var/eta_engine/state/btc_live/" in jarvis_supervised_paper_run
    assert "out of `docs/btc_live/`" not in jarvis_supervised_paper_run

    btc_live_docs_readme = _read("eta_engine/docs/btc_live/README.md")
    assert "not the authoritative live runtime surface" in btc_live_docs_readme
    assert "may still contain legacy path strings" in btc_live_docs_readme
    assert "var/eta_engine/state/btc_live/" in btc_live_docs_readme
    assert "var/eta_engine/state/broker_fleet/" in btc_live_docs_readme
    assert "var/eta_engine/state/btc_paper/" in btc_live_docs_readme

    btc_live_ecosystem_readme = _read("eta_engine/docs/btc_live/ecosystem/README.md")
    assert "not authoritative live runtime state" in btc_live_ecosystem_readme
    assert "var/eta_engine/state/btc_live/" in btc_live_ecosystem_readme
    assert "var/eta_engine/state/broker_fleet/" in btc_live_ecosystem_readme
    assert "var/eta_engine/state/btc_paper/" in btc_live_ecosystem_readme

    btc_live_broker_fleet_readme = _read("eta_engine/docs/btc_live/broker_fleet/README.md")
    assert "not the authoritative active fleet surface" in btc_live_broker_fleet_readme
    assert "var/eta_engine/state/broker_fleet/" in btc_live_broker_fleet_readme

    btc_live_control_readme = _read("eta_engine/docs/btc_live/control/README.md")
    assert "not authoritative live runtime state" in btc_live_control_readme
    assert "var/eta_engine/state/btc_live/" in btc_live_control_readme
    assert "var/eta_engine/state/broker_fleet/" in btc_live_control_readme

    btc_live_broker_connections_readme = _read("eta_engine/docs/btc_live/broker_connections/README.md")
    assert "not the authoritative active broker runtime surface" in btc_live_broker_connections_readme
    assert "canonical workspace runtime state" in btc_live_broker_connections_readme

    btc_inventory_readme = _read("eta_engine/docs/btc_inventory/README.md")
    assert "not authoritative live runtime state" in btc_inventory_readme
    assert "historical checked-in snapshot paths under" in btc_inventory_readme
    assert "docs/btc_live/" in btc_inventory_readme
    assert "var/eta_engine/state/btc_live/" in btc_inventory_readme
    assert "var/eta_engine/state/broker_fleet/" in btc_inventory_readme
    assert "var/eta_engine/state/btc_paper/" in btc_inventory_readme

    btc_paper_readme = _read("eta_engine/docs/btc_paper/README.md")
    assert "not authoritative live runtime state" in btc_paper_readme
    assert "btc_paper_journal.jsonl" in btc_paper_readme
    assert "var/eta_engine/state/btc_paper/" in btc_paper_readme
    assert "var/eta_engine/state/broker_fleet/" in btc_paper_readme

    broker_connections_readme = _read("eta_engine/docs/broker_connections/README.md")
    assert "historical checked-in broker/exchange connection probe" in broker_connections_readme
    assert "var/eta_engine/state/broker_connections/" in broker_connections_readme
    assert "historical/reference only" in broker_connections_readme
    assert "should not be re-staged into source history" in broker_connections_readme
    assert "python -m eta_engine.scripts.connect_brokers --probe" in broker_connections_readme
    assert "python scripts/connect_brokers.py" not in broker_connections_readme

    connect_brokers_script = _read("eta_engine/scripts/connect_brokers.py")
    assert "Thin CLI wrapper for broker connection probes" in connect_brokers_script
    assert "BrokerConnectionManager.from_env" in connect_brokers_script
    assert "write_broker_connection_report" in connect_brokers_script
    assert "var/eta_engine/state/broker_connections/" in connect_brokers_script
    assert "docs/broker_connections/" not in connect_brokers_script

    venues_connection = _read("eta_engine/venues/connection.py")
    assert "workspace_roots.ETA_BROKER_CONNECTION_REPORT_DIR" in venues_connection
    assert 'ROOT / "docs" / "broker_connections"' not in venues_connection

    preflight_script = _read("eta_engine/scripts/preflight.py")
    assert "workspace_roots.ETA_BROKER_CONNECTION_REPORT_DIR" in preflight_script
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in preflight_script
    assert 'ROOT / "docs" / "broker_connections"' not in preflight_script
    assert 'ROOT / "state"' not in preflight_script

    preflight_feed = _read("eta_engine/feeds/preflight.py")
    assert "workspace_roots.ETA_BROKER_CONNECTION_REPORT_DIR" in preflight_feed
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in preflight_feed
    assert 'ROOT / "docs" / "broker_connections"' not in preflight_feed
    assert 'ROOT / "state"' not in preflight_feed

    build_integrations_report = _read("eta_engine/scripts/build_integrations_report.py")
    assert "workspace_roots.ETA_INTEGRATIONS_REPORT_DIR" in build_integrations_report
    assert "workspace_roots.default_integrations_live_status_path()" in build_integrations_report
    assert "var/eta_engine/state/integrations/" in build_integrations_report
    assert 'ROOT / "docs"' not in build_integrations_report
    assert 'ROOT / "docs" / "integrations_live_status.json"' not in build_integrations_report

    build_integrations_report_feed = _read("eta_engine/feeds/build_integrations_report.py")
    assert "workspace_roots.ETA_INTEGRATIONS_REPORT_DIR" in build_integrations_report_feed
    assert "workspace_roots.default_integrations_live_status_path()" in build_integrations_report_feed
    assert "var/eta_engine/state/integrations/" in build_integrations_report_feed
    assert 'ROOT / "docs"' not in build_integrations_report_feed
    assert 'ROOT / "docs" / "integrations_live_status.json"' not in build_integrations_report_feed

    monthly_deep_review = _read("eta_engine/scripts/monthly_deep_review.py")
    assert "workspace_roots.ETA_MONTHLY_REVIEW_DIR" in monthly_deep_review
    assert "var/eta_engine/state/monthly_review/" in monthly_deep_review
    assert 'DEFAULT_OUT_DIR = ROOT / "docs"' not in monthly_deep_review

    monthly_deep_review_feed = _read("eta_engine/feeds/monthly_deep_review.py")
    assert "workspace_roots.ETA_MONTHLY_REVIEW_DIR" in monthly_deep_review_feed
    assert "var/eta_engine/state/monthly_review/" in monthly_deep_review_feed
    assert 'DEFAULT_OUT_DIR = ROOT / "docs"' not in monthly_deep_review_feed

    weekly_review = _read("eta_engine/scripts/weekly_review.py")
    assert "workspace_roots.ETA_WEEKLY_REVIEW_DIR" in weekly_review
    assert 'default=DEFAULT_OUT_DIR' in weekly_review
    assert 'default=ROOT / "docs"' not in weekly_review
    assert "var/eta_engine/state/weekly_review/weekly_review_log.json" in weekly_review
    assert "var/eta_engine/state/weekly_review/weekly_review_latest.json" in weekly_review
    assert "docs/weekly_review_log.json" not in weekly_review
    assert "docs/weekly_review_latest.json" not in weekly_review
    assert "weekly_review_log.json" in weekly_review
    assert "weekly_review_latest.json" in weekly_review

    schedule_weekly_review = _read("eta_engine/scripts/schedule_weekly_review.py")
    assert "workspace_roots.default_preflight_dryrun_report_path()" in schedule_weekly_review
    assert "workspace_roots.default_kill_log_path()" in schedule_weekly_review
    assert '"var" / "eta_engine" / "state" / "weekly_review" / "weekly_review_latest.json"' in schedule_weekly_review
    assert 'logs/eta_engine/weekly_review_cron.log' in schedule_weekly_review
    assert 'eta_engine/logs/weekly_review_cron.log' not in schedule_weekly_review

    schedule_weekly_review_feed = _read("eta_engine/feeds/schedule_weekly_review.py")
    assert "build_script_shim" in schedule_weekly_review_feed
    assert '"eta_engine.feeds.schedule_weekly_review"' in schedule_weekly_review_feed
    assert '"eta_engine.scripts.schedule_weekly_review"' in schedule_weekly_review_feed

    daily_premarket = _read("eta_engine/scripts/daily_premarket.py")
    assert "workspace_roots.default_premarket_inputs_path()" in daily_premarket
    assert "workspace_roots.ETA_PREMARKET_REPORT_DIR" in daily_premarket
    assert "var/eta_engine/state/premarket/" in daily_premarket
    assert 'ROOT / "docs" / "premarket_inputs.json"' not in daily_premarket
    assert 'DEFAULT_OUT_DIR = ROOT / "docs"' not in daily_premarket

    install_windows = _read("eta_engine/deploy/install_windows.ps1")
    assert "eta_engine.scripts.jarvis_live" in install_windows
    assert "--out-dir `\"$stateDir`\" --interval 60" in install_windows
    assert "--inputs docs\\premarket_inputs.json" not in install_windows

    register_tasks = _read("eta_engine/deploy/scripts/register_tasks.ps1")
    assert "eta_engine.scripts.jarvis_live" in register_tasks
    assert "--out-dir `\"$StateDir`\" --interval 60" in register_tasks
    assert "--inputs docs\\premarket_inputs.json" not in register_tasks

    live_launch_runbook = _read("eta_engine/docs/live_launch_runbook.md")
    assert "var/eta_engine/state/premarket/premarket_latest.json" in live_launch_runbook
    assert "var/eta_engine/state/premarket/premarket_latest.txt" in live_launch_runbook

    mnq_live_supervisor = _read("eta_engine/scripts/mnq_live_supervisor.py")
    assert "workspace_roots.ETA_MNQ_LIVE_STATE_DIR" in mnq_live_supervisor
    assert 'ROOT / "docs" / "mnq_live"' not in mnq_live_supervisor

    mnq_live_supervisor_feed = _read("eta_engine/feeds/mnq_live_supervisor.py")
    assert "workspace_roots.ETA_MNQ_LIVE_STATE_DIR" in mnq_live_supervisor_feed
    assert 'ROOT / "docs" / "mnq_live"' not in mnq_live_supervisor_feed

    dashboard_api = _read("eta_engine/deploy/scripts/dashboard_api.py")
    assert "workspace_roots.ETA_MNQ_LIVE_STATE_DIR" in dashboard_api
    assert "workspace_roots.ETA_LEGACY_MNQ_LIVE_STATE_DIR" in dashboard_api
    assert 'STATE_DIR.parent / "eta_engine" / "docs" / "mnq_live"' not in dashboard_api

    jarvis_live_service = _read("eta_engine/deploy/systemd/jarvis-live.service")
    assert "-m eta_engine.scripts.jarvis_live" in jarvis_live_service
    assert "__INSTALL_DIR__/../var/eta_engine/state" in jarvis_live_service
    assert "--inputs __INSTALL_DIR__/docs/premarket_inputs.json" not in jarvis_live_service

    roadmap_state = _read("eta_engine/roadmap_state.json")
    assert (
        "Historical note only: the docs/btc_live/broker_fleet tmp_path leak mentioned "
        "in this bundle was fixed in a later dashboard isolation batch."
    ) in roadmap_state
    assert "reads from the real docs/btc_live/broker_fleet instead of tmp_path" not in roadmap_state
    assert "Premarket and monthly review originally wrote checked-in docs snapshots in v0.1.25." in roadmap_state
    assert "var/eta_engine/state/monthly_review" in roadmap_state
    assert "var/eta_engine/state/weekly_review" in roadmap_state
    assert '"log_path": "var/eta_engine/state/weekly_review/weekly_review_log.json"' in roadmap_state
    assert '"log_path": "eta_engine/docs/weekly_review_log.json"' not in roadmap_state
    assert "var/eta_engine/state/premarket_inputs.json preferred, docs/premarket_inputs.json fallback" in roadmap_state
    assert "--inputs PATH (default docs/premarket_inputs.json)" not in roadmap_state
    assert "--out-dir PATH (default docs/)" not in roadmap_state

    roadmap_dashboard = _read("eta_engine/roadmap_dashboard.html")
    assert "var/eta_engine/state/decisions_v1.json" in roadmap_dashboard
    assert "docs/decisions_v1.json" not in roadmap_dashboard
    assert "var/eta_engine/state/paper_run/{paper_run_report.json,paper_run_tearsheet.txt}" in roadmap_dashboard
    assert "docs/paper_run_{report.json,tearsheet.txt}" not in roadmap_dashboard
    assert (
        "var/eta_engine/state/weekly_review/{weekly_review_log.json,"
        "weekly_review_latest.json,weekly_review_latest.txt}"
    ) in roadmap_dashboard
    assert "docs/weekly_review_{log.json,latest.json,latest.txt}" not in roadmap_dashboard
    assert "var/eta_engine/state/preflight/preflight_dryrun_report.json" in roadmap_dashboard
    assert "var/eta_engine/state/preflight/{preflight_dryrun_report.json,preflight_dryrun_log.txt}" in roadmap_dashboard
    assert "docs/preflight_dryrun_report.json" not in roadmap_dashboard
    assert "docs/preflight_dryrun_{report.json,log.txt}" not in roadmap_dashboard

    gitignore = _read("eta_engine/.gitignore")
    assert "docs/btc_live/control/*" in gitignore
    assert "!docs/btc_live/control/README.md" in gitignore

    config_json = _read("eta_engine/config.json")
    assert '"kill_log_path": "var/eta_engine/state/kill_log.json"' in config_json
    assert '"kill_log_path": "eta_engine/docs/kill_log.json"' not in config_json

    roadmap_md = _read("eta_engine/ROADMAP.md")
    assert "Kill log lives at `var/eta_engine/state/kill_log.json`." in roadmap_md
    assert "Kill log lives at `eta_engine/docs/kill_log.json`." not in roadmap_md

    architecture_md = _read("eta_engine/docs/ARCHITECTURE.md")
    assert "../var/eta_engine/state/kill_log.json" in architecture_md
    assert "Kill log (var/eta_engine/state/kill_log.json) or promotion to next gate" in architecture_md
    assert "Kill log (docs/kill_log.json) or promotion to next gate" not in architecture_md

    live_launch_runbook = _read("eta_engine/docs/live_launch_runbook.md")
    assert (
        "`var/eta_engine/state/kill_log.json` exists, valid JSON, has at least one "
        "review entry."
    ) in live_launch_runbook
    assert "Append kill reason + root cause to `var/eta_engine/state/kill_log.json`." in live_launch_runbook
    assert "`var/eta_engine/state/paper_run/paper_run_report.json` shows Tier-A (MNQ+NQ) PASS." in live_launch_runbook
    assert "`var/eta_engine/state/decisions_v1.json` exists with all 3 required tier sections." in live_launch_runbook
    assert "`docs/kill_log.json` exists, valid JSON, has at least one review entry." not in live_launch_runbook
    assert "Append kill reason + root cause to `docs/kill_log.json`." not in live_launch_runbook
    assert "`docs/paper_run_report.json` shows Tier-A (MNQ+NQ) PASS." not in live_launch_runbook
    assert "`docs/decisions_v1.json` exists with all 3 required tier sections." not in live_launch_runbook

    mnq_live_ops = _read("eta_engine/docs/mnq_live_operations_protocol.md")
    assert "Append root-cause entry to `var/eta_engine/state/kill_log.json`." in mnq_live_ops
    assert "ETA_PORTFOLIO_COMBINED_v1" in mnq_live_ops
    assert "MNQ_V2_FAMILY_CACHE_CONTAGION" in mnq_live_ops
    assert "Append root-cause entry to `docs/kill_log.json`." not in mnq_live_ops
    assert "var/eta_engine/state/paper_run/paper_run_report.json" in mnq_live_ops
    assert "python -m eta_engine.scripts.diamond_live_paper_drift --json" in mnq_live_ops
    assert "python -m eta_engine.scripts.mnq_latency_scorecard --hours 24 --json" in mnq_live_ops
    assert "eta_engine/scripts/diamond_live_paper_drift.py" in mnq_live_ops
    assert "eta_engine/scripts/mnq_latency_scorecard.py" in mnq_live_ops
    assert "docs/paper_run_report_mnq_only_v2.json" not in mnq_live_ops
    assert "session_scorecard_mnq.py" not in mnq_live_ops
    assert "eta_engine/scripts/live_vs_paper_drift.py" not in mnq_live_ops
    assert "eta_engine/docs/mnq_v2_trades.json" not in mnq_live_ops
    assert "`docs/kill_log.json`" not in mnq_live_ops

    decision_journal = _read("eta_engine/obs/decision_journal.py")
    assert "var/eta_engine/state/kill_log.json" in decision_journal
    assert "var/eta_engine/state/decisions_v1.json" in decision_journal
    assert "docs/kill_log.json" not in decision_journal
    assert "docs/decisions_v1.json" not in decision_journal

    bump_roadmap_v0_1_62 = _read("eta_engine/scripts/_bump_roadmap_v0_1_62.py")
    assert "stale broker_fleet state outside tmp_path" in bump_roadmap_v0_1_62
    assert "docs/btc_live/broker_" not in bump_roadmap_v0_1_62

    bump_roadmap_v0_1_62_feed = _read("eta_engine/feeds/_bump_roadmap_v0_1_62.py")
    assert "stale broker_fleet state outside tmp_path" in bump_roadmap_v0_1_62_feed
    assert "docs/btc_live/broker_" not in bump_roadmap_v0_1_62_feed

    fleet_sweep = _read("eta_engine/scripts/fleet_sweep.py")
    assert "workspace_roots.ETA_PAPER_SOAK_LEDGER_PATH" in fleet_sweep
    assert 'workspace_roots.ETA_ENGINE_ROOT / "strategies" / "per_bot_registry.py"' in fleet_sweep
    assert 'Path("var/eta_engine/state/paper_soak_ledger.json")' not in fleet_sweep
    assert 'Path("eta_engine/strategies/per_bot_registry.py")' not in fleet_sweep

    repo_health = _read("eta_engine/scripts/_repo_health.py")
    assert "workspace_roots.ETA_JARVIS_LIVE_LOG_PATH" in repo_health
    assert 'ROOT / "docs" / "jarvis_live_log.jsonl"' not in repo_health

    repo_health_feed = _read("eta_engine/feeds/_repo_health.py")
    assert "eta_engine.scripts._repo_health" in repo_health_feed
    assert "Compatibility shim" in repo_health_feed
    mnq_latency_scorecard = _read("eta_engine/scripts/mnq_latency_scorecard.py")
    assert "workspace_roots.default_alerts_log_path()" in mnq_latency_scorecard
    assert "closed_trade_ledger.load_close_records" in mnq_latency_scorecard
    assert 'ROOT / "docs" / "alerts_log.jsonl"' not in mnq_latency_scorecard
    session_scorecard = _read("eta_engine/scripts/session_scorecard_mnq.py")
    assert "eta_engine.scripts.mnq_latency_scorecard" in session_scorecard
    assert "Compatibility wrapper" in session_scorecard
    dormancy_audit = _read("eta_engine/scripts/_audit_dormancy_consistency.py")
    assert '"scripts/live_vs_paper_drift.py"' not in dormancy_audit

    dormancy_audit_feed = _read("eta_engine/feeds/_audit_dormancy_consistency.py")
    assert '"scripts/live_vs_paper_drift.py"' not in dormancy_audit_feed

    bump_roadmap = _read("eta_engine/scripts/_bump_roadmap_v0_1_28.py")
    assert "var/eta_engine/state/jarvis_live_health.json" in bump_roadmap
    assert "var/eta_engine/state/jarvis_live_log.jsonl" in bump_roadmap
    assert "var/eta_engine/state/premarket_inputs.json preferred" in bump_roadmap
    assert "--inputs PATH (default docs/premarket_inputs.json)" not in bump_roadmap
    assert "--out-dir PATH (default docs/)" not in bump_roadmap
    assert "docs/jarvis_live_health.json" not in bump_roadmap
    assert "docs/jarvis_live_log.jsonl" not in bump_roadmap

    bump_roadmap_feed = _read("eta_engine/feeds/_bump_roadmap_v0_1_28.py")
    assert "var/eta_engine/state/jarvis_live_health.json" in bump_roadmap_feed
    assert "var/eta_engine/state/jarvis_live_log.jsonl" in bump_roadmap_feed
    assert "var/eta_engine/state/premarket_inputs.json preferred" in bump_roadmap_feed
    assert "--inputs PATH (default docs/premarket_inputs.json)" not in bump_roadmap_feed
    assert "--out-dir PATH (default docs/)" not in bump_roadmap_feed
    assert "docs/jarvis_live_health.json" not in bump_roadmap_feed
    assert "docs/jarvis_live_log.jsonl" not in bump_roadmap_feed

    bump_roadmap_v0_1_25 = _read("eta_engine/scripts/_bump_roadmap_v0_1_25.py")
    assert "Historical note: premarket and monthly review later moved to canonical" in bump_roadmap_v0_1_25
    assert "var/eta_engine/state/monthly_review" in bump_roadmap_v0_1_25
    assert "var/eta_engine/state/weekly_review" in bump_roadmap_v0_1_25
    assert "Historical note only: data_sources.md documented" in bump_roadmap_v0_1_25

    bump_roadmap_v0_1_25_feed = _read("eta_engine/feeds/_bump_roadmap_v0_1_25.py")
    assert "Historical note: premarket and monthly review later moved to canonical" in bump_roadmap_v0_1_25_feed
    assert "var/eta_engine/state/monthly_review" in bump_roadmap_v0_1_25_feed
    assert "var/eta_engine/state/weekly_review" in bump_roadmap_v0_1_25_feed
    assert "Historical note only: data_sources.md documented" in bump_roadmap_v0_1_25_feed


def test_kaizen_and_hermes_runtime_surfaces_use_workspace_root_helpers() -> None:
    kaizen_loop = _read("eta_engine/scripts/kaizen_loop.py")
    assert "workspace_roots.ETA_KAIZEN_REPORT_DIR" in kaizen_loop
    assert "workspace_roots.ETA_KAIZEN_LATEST_PATH" in kaizen_loop
    assert "workspace_roots.ETA_KAIZEN_ACTIONS_LOG_PATH" in kaizen_loop
    assert "workspace_roots.ETA_KAIZEN_OVERRIDES_PATH" in kaizen_loop
    assert "workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH" in kaizen_loop
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_reports")' not in kaizen_loop
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_actions.jsonl")' not in kaizen_loop
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_overrides.json")' not in kaizen_loop
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_actions.jsonl")' not in kaizen_loop

    kaizen_reactivate = _read("eta_engine/scripts/kaizen_reactivate.py")
    assert "workspace_roots.ETA_KAIZEN_OVERRIDES_PATH" in kaizen_reactivate
    assert "workspace_roots.ETA_KAIZEN_REACTIVATE_LOG_PATH" in kaizen_reactivate
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_overrides.json")' not in kaizen_reactivate
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\kaizen_reactivate.log")' not in kaizen_reactivate

    hermes_bridge_health = _read("eta_engine/scripts/hermes_bridge_health.py")
    assert "workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH" in hermes_bridge_health
    assert "workspace_roots.ETA_HERMES_MEMORY_DB_PATH" in hermes_bridge_health
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_actions.jsonl")' not in hermes_bridge_health
    assert (
        r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_memory_store.db")'
    ) not in hermes_bridge_health

    hermes_memory_backup = _read("eta_engine/scripts/hermes_memory_backup.py")
    assert "workspace_roots.ETA_HERMES_MEMORY_DB_PATH" in hermes_memory_backup
    assert "workspace_roots.ETA_HERMES_MEMORY_BACKUP_DIR" in hermes_memory_backup
    assert (
        r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\hermes_memory_store.db")'
    ) not in hermes_memory_backup
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\backups\hermes_memory")' not in hermes_memory_backup

    fm_cost_rollup = _read("eta_engine/scripts/fm_cost_rollup.py")
    assert "workspace_roots.ETA_MULTI_MODEL_TELEMETRY_LOG_PATH" in fm_cost_rollup
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\multi_model_telemetry.jsonl")' not in fm_cost_rollup

    auto_cutover_watcher = _read("eta_engine/scripts/auto_cutover_watcher.py")
    assert 'workspace_roots.ETA_ENGINE_ROOT / ".env"' in auto_cutover_watcher
    assert "workspace_roots.ETA_CUTOVER_STATUS_PATH" in auto_cutover_watcher
    assert "workspace_roots.ETA_RUNTIME_LOG_DIR" in auto_cutover_watcher
    assert r'Path(r"C:\EvolutionaryTradingAlgo\eta_engine\.env")' not in auto_cutover_watcher
    assert r'C:\EvolutionaryTradingAlgo\var\eta_engine\state\cutover_status.json' not in auto_cutover_watcher
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\logs")' not in auto_cutover_watcher

    project_kaizen_closeout = _read("eta_engine/scripts/project_kaizen_closeout.py")
    assert "_CANONICAL_ROOT = workspace_roots.WORKSPACE_ROOT" in project_kaizen_closeout
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in project_kaizen_closeout
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in project_kaizen_closeout


def test_telegram_and_anomaly_operator_surfaces_use_workspace_root_helpers() -> None:
    anomaly_watcher = _read("eta_engine/brain/jarvis_v3/anomaly_watcher.py")
    assert "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH" in anomaly_watcher
    assert "workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH" in anomaly_watcher
    assert "workspace_roots.ETA_ANOMALY_HITS_LOG_PATH" in anomaly_watcher
    assert "workspace_roots.ETA_LEGACY_ANOMALY_HITS_LOG_PATH" in anomaly_watcher
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in anomaly_watcher

    anomaly_pulse = _read("eta_engine/scripts/anomaly_telegram_pulse.py")
    assert "workspace_roots.ETA_ANOMALY_PULSE_LOG_PATH" in anomaly_pulse
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in anomaly_pulse

    telegram_inbound = _read("eta_engine/scripts/telegram_inbound_bot.py")
    assert "workspace_roots.ETA_TELEGRAM_INBOUND_OFFSET_PATH" in telegram_inbound
    assert "workspace_roots.ETA_TELEGRAM_SILENCE_UNTIL_PATH" in telegram_inbound
    assert "workspace_roots.ETA_TELEGRAM_INBOUND_AUDIT_LOG_PATH" in telegram_inbound
    assert "workspace_roots.ETA_TELEGRAM_HERMES_LAST_CHAT_PATH" in telegram_inbound
    assert "workspace_roots.ETA_ANOMALY_HITS_LOG_PATH" in telegram_inbound
    assert "workspace_roots.ETA_HERMES_STATE_PATH" in telegram_inbound
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in telegram_inbound
    assert r'C:\EvolutionaryTradingAlgo\var\telegram_inbound_offset.json' not in telegram_inbound

    proactive = _read("eta_engine/scripts/hermes_proactive_investigator.py")
    assert "workspace_roots.ETA_ANOMALY_HITS_LOG_PATH" in proactive
    assert "workspace_roots.ETA_HERMES_PROACTIVE_CURSOR_PATH" in proactive
    assert "workspace_roots.ETA_HERMES_PROACTIVE_AUDIT_PATH" in proactive
    assert "workspace_roots.ETA_LEGACY_ANOMALY_HITS_LOG_PATH" in proactive
    assert "workspace_roots.ETA_LEGACY_HERMES_PROACTIVE_CURSOR_PATH" in proactive
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in proactive

    cost_tracker = _read("eta_engine/brain/jarvis_v3/cost_tracker.py")
    assert "workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH" in cost_tracker
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in cost_tracker

    hermes_voice = _read("eta_engine/desktop/hermes_voice.py")
    assert "workspace_roots.ETA_HERMES_VOICE_LOG_PATH" in hermes_voice
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\hermes_voice.log")' not in hermes_voice


def test_read_side_analytics_and_preflight_use_workspace_root_helpers() -> None:
    workspace_roots_text = _read("eta_engine/scripts/workspace_roots.py")
    assert 'ETA_PROP_FIRM_ACCOUNT_MAP_PATH = ETA_RUNTIME_STATE_DIR / "prop_firm_accounts.json"' in workspace_roots_text
    assert 'ETA_DAILY_DEBRIEF_LOG_PATH = ETA_RUNTIME_LOG_DIR / "daily_debrief.jsonl"' in workspace_roots_text
    assert 'ETA_PREFLIGHT_RUNS_LOG_PATH = ETA_RUNTIME_LOG_DIR / "preflight_runs.jsonl"' in workspace_roots_text
    assert 'ETA_LEGACY_TELEGRAM_INBOUND_LOG_PATH = ROOT_VAR_DIR / "telegram_inbound.log"' in workspace_roots_text
    assert 'ETA_LEGACY_TELEGRAM_INBOUND_ERR_PATH = ROOT_VAR_DIR / "telegram_inbound.err"' in workspace_roots_text

    attribution_cube = _read("eta_engine/brain/jarvis_v3/attribution_cube.py")
    assert "workspace_roots.ETA_JARVIS_TRACE_PATH" in attribution_cube
    assert "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH" in attribution_cube
    assert "workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH" in attribution_cube
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in attribution_cube

    kelly_optimizer = _read("eta_engine/brain/jarvis_v3/kelly_optimizer.py")
    assert "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH" in kelly_optimizer
    assert "workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH" in kelly_optimizer
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in kelly_optimizer

    risk_topology = _read("eta_engine/brain/jarvis_v3/risk_topology.py")
    assert "workspace_roots.ETA_KAIZEN_LATEST_PATH" in risk_topology
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in risk_topology

    pnl_summary = _read("eta_engine/brain/jarvis_v3/pnl_summary.py")
    assert "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH" in pnl_summary
    assert "workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH" in pnl_summary
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in pnl_summary

    prop_firm_guardrails = _read("eta_engine/brain/jarvis_v3/prop_firm_guardrails.py")
    assert "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH" in prop_firm_guardrails
    assert "workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH" in prop_firm_guardrails
    assert "workspace_roots.ETA_PROP_FIRM_ACCOUNT_MAP_PATH" in prop_firm_guardrails
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in prop_firm_guardrails

    preflight = _read("eta_engine/brain/jarvis_v3/preflight.py")
    assert "workspace_roots.WORKSPACE_ROOT" in preflight
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in preflight
    assert "workspace_roots.ETA_JARVIS_TRADE_CLOSES_PATH" in preflight
    assert "workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH" in preflight
    assert "workspace_roots.ETA_HERMES_MEMORY_BACKUP_DIR" in preflight
    assert "workspace_roots.ETA_KAIZEN_LATEST_PATH" in preflight
    assert "workspace_roots.ETA_HERMES_STATE_PATH" in preflight
    assert "workspace_roots.ETA_KAIZEN_OVERRIDES_PATH" in preflight
    assert "workspace_roots.ETA_ANOMALY_HITS_LOG_PATH" in preflight
    assert "workspace_roots.ETA_LEGACY_ANOMALY_HITS_LOG_PATH" in preflight
    assert "workspace_roots.ETA_PREFLIGHT_RUNS_LOG_PATH" in preflight
    assert "workspace_roots.ETA_TELEGRAM_INBOUND_OFFSET_PATH" in preflight
    assert "workspace_roots.ETA_TELEGRAM_INBOUND_AUDIT_LOG_PATH" in preflight
    assert "workspace_roots.ETA_LEGACY_TELEGRAM_INBOUND_LOG_PATH" in preflight
    assert "workspace_roots.ETA_LEGACY_TELEGRAM_INBOUND_ERR_PATH" in preflight
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in preflight

    daily_debrief = _read("eta_engine/scripts/daily_debrief.py")
    assert "workspace_roots.ETA_DAILY_DEBRIEF_LOG_PATH" in daily_debrief
    assert "workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH" in daily_debrief
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in daily_debrief

    bridge_preflight = _read("eta_engine/scripts/bridge_preflight.py")
    assert "workspace_roots.WORKSPACE_ROOT" in bridge_preflight
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in bridge_preflight
    assert "workspace_roots.ETA_HERMES_MEMORY_BACKUP_DIR" in bridge_preflight
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in bridge_preflight


def test_operator_reporting_and_cutover_helpers_use_workspace_root_helpers() -> None:
    workspace_roots_text = _read("eta_engine/scripts/workspace_roots.py")
    assert (
        'ETA_BRIDGE_AUTOHEAL_ACTIONS_LOG_PATH = ROOT_VAR_DIR / '
        '"bridge_autoheal_actions.jsonl"'
    ) in workspace_roots_text
    assert (
        'ETA_HERMES_EVENING_JOURNAL_AUDIT_PATH = ROOT_VAR_DIR / '
        '"hermes_evening_journal.jsonl"'
    ) in workspace_roots_text

    bridge_autoheal = _read("eta_engine/scripts/bridge_autoheal.py")
    assert "workspace_roots.WORKSPACE_ROOT" in bridge_autoheal
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in bridge_autoheal
    assert "workspace_roots.ETA_BRIDGE_AUTOHEAL_ACTIONS_LOG_PATH" in bridge_autoheal
    assert "workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH" in bridge_autoheal
    assert "workspace_roots.ETA_HERMES_MEMORY_BACKUP_DIR" in bridge_autoheal
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in bridge_autoheal

    eta_status = _read("eta_engine/scripts/eta_status.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in eta_status
    assert "workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH" in eta_status
    assert "workspace_roots.ETA_DIAMOND_LEADERBOARD_PATH" in eta_status
    assert "workspace_roots.ETA_DIAMOND_PROP_LAUNCH_READINESS_PATH" in eta_status
    assert "workspace_roots.ETA_KAIZEN_LATEST_PATH" in eta_status
    assert "workspace_roots.ETA_ETA_EVENTS_LOG_PATH" in eta_status
    assert "workspace_roots.ETA_QUANTUM_STATE_DIR" in eta_status
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in eta_status
    assert 'STATE_DIR = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"' not in eta_status

    hermes_evening_journal = _read("eta_engine/scripts/hermes_evening_journal.py")
    assert "workspace_roots.ETA_HERMES_EVENING_JOURNAL_AUDIT_PATH" in hermes_evening_journal
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in hermes_evening_journal

    hermes_admin_audit = _read("eta_engine/scripts/jarvis_hermes_admin_audit.py")
    assert "workspace_roots.WORKSPACE_ROOT" in hermes_admin_audit
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in hermes_admin_audit

    monday_first_light = _read("eta_engine/scripts/monday_first_light_check.py")
    assert "workspace_roots.WORKSPACE_ROOT" in monday_first_light
    assert "workspace_roots.ETA_RUNTIME_HEALTH_DIR" in monday_first_light
    assert "workspace_roots.ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH" in monday_first_light
    assert "workspace_roots.ETA_DIAMOND_PROP_DRAWDOWN_GUARD_PATH" in monday_first_light
    assert "workspace_roots.ETA_JARVIS_SHADOW_SIGNALS_PATH" in monday_first_light
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in monday_first_light


def test_mcp_server_and_reviewer_use_workspace_root_helpers() -> None:
    workspace_roots_text = _read("eta_engine/scripts/workspace_roots.py")
    assert 'ETA_DASHBOARD_EVENTS_PATH = ETA_RUNTIME_STATE_DIR / "dashboard_events.jsonl"' in workspace_roots_text
    assert 'ETA_UPTIME_EVENTS_PATH = ETA_RUNTIME_STATE_DIR / "uptime_events.jsonl"' in workspace_roots_text

    mcp_server = _read("eta_engine/mcp_servers/jarvis_mcp_server.py")
    assert "workspace_roots.WORKSPACE_ROOT" in mcp_server
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in mcp_server
    assert "workspace_roots.ETA_HERMES_ACTIONS_LOG_PATH" in mcp_server
    assert "workspace_roots.ETA_KAIZEN_ACTIONS_LOG_PATH" in mcp_server
    assert "workspace_roots.ETA_KAIZEN_OVERRIDES_PATH" in mcp_server
    assert "workspace_roots.ETA_HERMES_STATE_PATH" in mcp_server
    assert "workspace_roots.ETA_KAIZEN_LATEST_PATH" in mcp_server
    assert "workspace_roots.ETA_JARVIS_TRACE_PATH" in mcp_server
    assert "workspace_roots.ETA_DASHBOARD_EVENTS_PATH" in mcp_server
    assert "workspace_roots.ETA_RUNTIME_DECISION_JOURNAL_PATH" in mcp_server
    assert "workspace_roots.ETA_JARVIS_V3_EVENTS_PATH" in mcp_server
    assert "workspace_roots.ETA_UPTIME_EVENTS_PATH" in mcp_server
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in mcp_server
    assert '_STATE_ROOT / "dashboard_events.jsonl"' not in mcp_server

    adversarial_reviewer = _read("eta_engine/feeds/adversarial_reviewer.py")
    assert "workspace_roots.ETA_ENGINE_ROOT / \".env\"" in adversarial_reviewer
    assert "workspace_roots.WORKSPACE_ROOT / \".env\"" in adversarial_reviewer
    assert r'Path(r"C:\EvolutionaryTradingAlgo\eta_engine\.env")' not in adversarial_reviewer


def test_archive_cleanup_and_strategy_gauntlet_use_single_canonical_script_surface() -> None:
    archive_script = _read("eta_engine/scripts/auto_archive_cleanup.py")
    archive_feed = _read("eta_engine/feeds/auto_archive_cleanup.py")
    assert "workspace_roots.WORKSPACE_ROOT" in archive_script
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in archive_script
    assert "eta_engine.scripts.auto_archive_cleanup" in archive_feed
    assert "Compatibility shim" in archive_feed

    gauntlet_script = _read("eta_engine/scripts/strategy_gauntlet.py")
    gauntlet_feed = _read("eta_engine/feeds/strategy_gauntlet.py")
    assert "workspace_roots.WORKSPACE_ROOT" in gauntlet_script
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in gauntlet_script
    assert "eta_engine.scripts.strategy_gauntlet" in gauntlet_feed
    assert "Compatibility shim" in gauntlet_feed


def test_deploy_helper_scripts_use_workspace_root_helpers() -> None:
    canonical_vps_fix = _read("eta_engine/deploy/canonical_vps_fix.py")
    assert "workspace_roots.WORKSPACE_ROOT" in canonical_vps_fix
    assert 'HEALTH_SCRIPT = workspace_roots.ETA_ENGINE_ROOT / "scripts" / "health_check.py"' in canonical_vps_fix
    assert (
        'HEALTH_OUTPUT_DIR = workspace_roots.WORKSPACE_ROOT / "firm_command_center" / '
        '"var" / "health"'
    ) in canonical_vps_fix
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in canonical_vps_fix

    promote_btc = _read("eta_engine/deploy/scripts/promote_btc.py")
    assert 'workspace_roots.ETA_ENGINE_ROOT / "strategies" / "per_bot_registry.py"' in promote_btc
    assert r'Path(r"C:\EvolutionaryTradingAlgo\eta_engine\strategies\per_bot_registry.py")' not in promote_btc

    supercharge_fleet = _read("eta_engine/deploy/scripts/supercharge_fleet.py")
    assert 'workspace_roots.ETA_ENGINE_ROOT / "strategies" / "per_bot_registry.py"' in supercharge_fleet
    assert r'Path(r"C:\EvolutionaryTradingAlgo\eta_engine\strategies\per_bot_registry.py")' not in supercharge_fleet


def test_data_and_dev_helper_scripts_use_workspace_root_helpers() -> None:
    sage_oracle = _read("eta_engine/scripts/sage_oracle.py")
    assert "workspace_roots.CRYPTO_IBKR_HISTORY_ROOT" in sage_oracle
    assert 'workspace_roots.WORKSPACE_ROOT / "data" / "MNQ_5m.csv"' in sage_oracle
    assert 'workspace_roots.WORKSPACE_ROOT / "data" / "NQ_5m.csv"' in sage_oracle
    assert r'Path(r"C:\EvolutionaryTradingAlgo\data\crypto\ibkr\history\BTC_1h.csv")' not in sage_oracle
    assert r'Path(r"C:\EvolutionaryTradingAlgo\data")' not in sage_oracle

    oos_validation = _read("eta_engine/scripts/oos_validation.py")
    assert "ROOT = workspace_roots.ETA_ENGINE_ROOT" in oos_validation
    assert 'workspace_roots.WORKSPACE_ROOT / "reports" / "oos_validation"' in oos_validation
    assert r'Path(r"C:\EvolutionaryTradingAlgo\reports\oos_validation")' not in oos_validation

    check_micro_bars = _read("eta_engine/dev/check_micro_bars.py")
    assert "workspace_roots.MNQ_HISTORY_ROOT" in check_micro_bars
    assert r'Path(r"C:\EvolutionaryTradingAlgo\mnq_data\history")' not in check_micro_bars

    check_rehab_bars = _read("eta_engine/dev/check_rehab_bars.py")
    assert "workspace_roots.MNQ_HISTORY_ROOT" in check_rehab_bars
    assert r'Path(r"C:\EvolutionaryTradingAlgo\mnq_data\history")' not in check_rehab_bars

    summarize_audit = _read("eta_engine/dev/summarize_audit.py")
    assert 'workspace_roots.ETA_ENGINE_ROOT / "reports" / "strict_gate_post_microtier.json"' in summarize_audit
    assert (
        r'Path(r"C:\EvolutionaryTradingAlgo\eta_engine\reports\strict_gate_post_microtier.json")'
    ) not in summarize_audit


def test_paper_run_and_repo_health_helpers_use_workspace_root_helpers() -> None:
    paper_run_harness = _read("eta_engine/scripts/paper_run_harness.py")
    assert "workspace_roots.ETA_PAPER_RUN_DIR" in paper_run_harness
    assert "var/eta_engine/state/paper_run/paper_run_report.json" in paper_run_harness
    assert "var/eta_engine/state/paper_run/paper_run_tearsheet.txt" in paper_run_harness
    assert 'default=ROOT / "docs"' not in paper_run_harness

    sharpe_drift = _read("eta_engine/scripts/_sharpe_drift.py")
    assert "workspace_roots.default_paper_run_report_path()" in sharpe_drift
    assert "workspace_roots.ETA_SHARPE_BASELINE_PATH" in sharpe_drift
    assert 'ROOT / "docs" / "paper_run_report.json"' not in sharpe_drift
    assert 'ROOT / "docs" / "sharpe_baseline.json"' not in sharpe_drift

    sharpe_drift_feed = _read("eta_engine/feeds/_sharpe_drift.py")
    assert "eta_engine.scripts._sharpe_drift" in sharpe_drift_feed
    assert "Compatibility shim" in sharpe_drift_feed

    repo_health = _read("eta_engine/scripts/_repo_health.py")
    assert "workspace_roots.default_decisions_v1_path()" in repo_health
    assert 'ROOT / "docs" / "decisions_v1.json"' not in repo_health

    repo_health_feed = _read("eta_engine/feeds/_repo_health.py")
    assert "eta_engine.scripts._repo_health" in repo_health_feed
    assert "Compatibility shim" in repo_health_feed
    dormancy_audit = _read("eta_engine/scripts/_audit_dormancy_consistency.py")
    assert '"scripts/live_vs_paper_drift.py"' not in dormancy_audit

    dormancy_audit_feed = _read("eta_engine/feeds/_audit_dormancy_consistency.py")
    assert '"scripts/live_vs_paper_drift.py"' not in dormancy_audit_feed


def test_supervisor_env_loader_and_dashboard_fix_use_workspace_root_helpers() -> None:
    supervisor = _read("eta_engine/scripts/jarvis_strategy_supervisor.py")
    assert 'workspace_roots.ROOT_VAR_DIR / "eta_engine" / ".env"' in supervisor
    assert 'workspace_roots.ETA_ENGINE_ROOT / ".env"' in supervisor
    assert 'workspace_roots.WORKSPACE_ROOT / ".env"' in supervisor
    assert r'Path(r"C:\EvolutionaryTradingAlgo\var\eta_engine\.env")' not in supervisor
    assert r'Path(r"C:\EvolutionaryTradingAlgo\eta_engine\.env")' not in supervisor
    assert r'Path(r"C:\EvolutionaryTradingAlgo\.env")' not in supervisor

    dashboard_fix = _read("eta_engine/fix_dashboard.py")
    assert "workspace_roots.WORKSPACE_ROOT" in dashboard_fix
    assert r'Path(r"C:\EvolutionaryTradingAlgo")' not in dashboard_fix


def test_coordination_and_broker_runtime_helpers_use_workspace_root_helpers() -> None:
    agent_coordinator = _read("eta_engine/scripts/agent_coordinator.py")
    assert "workspace_roots.WORKSPACE_ROOT" in agent_coordinator
    assert 'Path("C:/EvolutionaryTradingAlgo")' not in agent_coordinator

    ibkr_venue = _read("eta_engine/venues/ibkr.py")
    assert "workspace_roots.WORKSPACE_ROOT" in ibkr_venue
    assert r'C:\EvolutionaryTradingAlgo\firm_command_center' not in ibkr_venue

    tastytrade_venue = _read("eta_engine/venues/tastytrade.py")
    assert "workspace_roots.WORKSPACE_ROOT" in tastytrade_venue
    assert r'C:\EvolutionaryTradingAlgo\firm_command_center' not in tastytrade_venue


def test_market_data_and_feed_defaults_use_workspace_root_helpers() -> None:
    alpaca_venue = _read("eta_engine/venues/alpaca.py")
    assert "workspace_roots.WORKSPACE_ROOT" in alpaca_venue
    assert r'C:\EvolutionaryTradingAlgo\firm_command_center' not in alpaca_venue

    cme_basis_provider = _read("eta_engine/feeds/cme_basis_provider.py")
    assert 'workspace_roots.CRYPTO_HISTORY_ROOT / "BTC_5m.csv"' in cme_basis_provider
    assert 'workspace_roots.CRYPTO_HISTORY_ROOT / "ETH_5m.csv"' in cme_basis_provider
    assert r'C:\EvolutionaryTradingAlgo\data\crypto\history\BTC_5m.csv' not in cme_basis_provider

    sage_gated_orb = _read("eta_engine/strategies/sage_gated_orb_strategy.py")
    assert 'workspace_roots.MNQ_HISTORY_ROOT / "VIX_5m.csv"' in sage_gated_orb
    assert r'C:\EvolutionaryTradingAlgo\mnq_data\history\VIX_5m.csv' not in sage_gated_orb

    strategy_lab_engine = _read("eta_engine/feeds/strategy_lab/engine.py")
    assert 'Path(os.environ.get("ETA_WORKSPACE", str(workspace_roots.WORKSPACE_ROOT)))' in strategy_lab_engine
    assert r'Path(os.environ.get("ETA_WORKSPACE", r"C:\EvolutionaryTradingAlgo"))' not in strategy_lab_engine

    feed_tastytrade = _read("eta_engine/feeds/tastytrade.py")
    assert '_TARGET_NAME = "eta_engine.venues.tastytrade"' in feed_tastytrade
    feed_tastytrade_impl = _read("eta_engine/venues/tastytrade.py")
    assert "workspace_roots.WORKSPACE_ROOT" in feed_tastytrade_impl
    assert r'C:\EvolutionaryTradingAlgo\firm_command_center' not in feed_tastytrade_impl


def test_runtime_safety_and_alert_defaults_use_workspace_root_helpers() -> None:
    position_reconciler = _read("eta_engine/obs/position_reconciler.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in position_reconciler
    assert 'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state")' not in position_reconciler

    execution_lease = _read("eta_engine/safety/execution_lease.py")
    assert 'workspace_roots.ETA_RUNTIME_STATE_DIR / "execution_leases"' in execution_lease
    assert 'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/execution_leases")' not in execution_lease

    drift_alarm_alerter = _read("eta_engine/scripts/drift_alarm_alerter.py")
    assert 'Path(os.environ.get("ETA_WORKSPACE_ROOT", str(workspace_roots.WORKSPACE_ROOT)))' in drift_alarm_alerter
    assert r'Path(os.environ.get("ETA_WORKSPACE_ROOT", r"C:\EvolutionaryTradingAlgo"))' not in drift_alarm_alerter

    adversarial_reviewer = _read("eta_engine/feeds/adversarial_reviewer.py")
    assert 'workspace_roots.WORKSPACE_ROOT / "reports" / "strategy_reviews"' in adversarial_reviewer
    assert 'Path("C:/EvolutionaryTradingAlgo/reports/strategy_reviews")' not in adversarial_reviewer


def test_data_quality_helper_defaults_use_workspace_root_helpers() -> None:
    bar_accumulator = _read("eta_engine/feeds/bar_accumulator.py")
    assert 'workspace_roots.WORKSPACE_ROOT / "data"' in bar_accumulator
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in bar_accumulator
    assert 'Path("C:/EvolutionaryTradingAlgo/data")' not in bar_accumulator
    assert 'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state")' not in bar_accumulator

    data_quality_monitor = _read("eta_engine/feeds/data_quality_monitor.py")
    assert 'workspace_roots.WORKSPACE_ROOT / "data"' in data_quality_monitor
    assert 'workspace_roots.ETA_RUNTIME_STATE_DIR / "data_health" / "feed_health.json"' in data_quality_monitor
    assert 'Path("C:/EvolutionaryTradingAlgo/data")' not in data_quality_monitor
    assert (
        'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/data_health/feed_health.json")'
    ) not in data_quality_monitor

    verdict_miner = _read("eta_engine/feeds/verdict_miner.py")
    assert 'workspace_roots.ETA_RUNTIME_STATE_DIR / "jarvis_live_log.jsonl"' in verdict_miner
    assert 'workspace_roots.WORKSPACE_ROOT / "reports" / "verdict_patterns" / "daily_report.json"' in verdict_miner
    assert 'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_live_log.jsonl")' not in verdict_miner
    assert 'Path("C:/EvolutionaryTradingAlgo/reports/verdict_patterns/daily_report.json")' not in verdict_miner

    validate_bar_data_hygiene = _read("eta_engine/scripts/validate_bar_data_hygiene.py")
    assert "default=str(workspace_roots.WORKSPACE_ROOT)" in validate_bar_data_hygiene
    assert 'default=str(Path("C:/EvolutionaryTradingAlgo"))' not in validate_bar_data_hygiene


def test_status_server_and_avengers_watchdog_use_workspace_root_helpers() -> None:
    jarvis_status_server = _read("eta_engine/scripts/jarvis_status_server.py")
    assert 'os.environ.get("PYTHONPATH", str(workspace_roots.WORKSPACE_ROOT))' in jarvis_status_server
    assert r'os.environ.get("PYTHONPATH", r"C:\EvolutionaryTradingAlgo")' not in jarvis_status_server

    avengers_watchdog = _read("eta_engine/brain/avengers/watchdog.py")
    assert 'workspace_roots.WORKSPACE_ROOT / "launchers"' in avengers_watchdog
    assert 'Path("C:/EvolutionaryTradingAlgo/launchers")' not in avengers_watchdog


def test_regime_detector_and_strategy_lab_defaults_use_workspace_root_helpers() -> None:
    regime_detector = _read("eta_engine/feeds/regime_detector/detector.py")
    assert 'workspace_roots.WORKSPACE_ROOT / "data"' in regime_detector
    assert "workspace_roots.ETA_REGIME_STATE_PATH" in regime_detector
    assert 'Path("C:/EvolutionaryTradingAlgo/data")' not in regime_detector
    assert (
        'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_intel/regime_state.json")'
    ) not in regime_detector

    run_regime = _read("eta_engine/feeds/regime_detector/run_regime.py")
    assert 'workspace_roots.WORKSPACE_ROOT / "data"' in run_regime
    assert "workspace_roots.ETA_REGIME_STATE_PATH" in run_regime
    assert 'Path("C:/EvolutionaryTradingAlgo/data")' not in run_regime
    assert 'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_intel/regime_state.json")' not in run_regime

    strategy_lab_app = _read("eta_engine/feeds/strategy_lab/app.py")
    assert 'str(workspace_roots.WORKSPACE_ROOT / "data")' in strategy_lab_app
    assert 'workspace_roots.WORKSPACE_ROOT / "reports" / "lab_reports"' in strategy_lab_app
    assert 'C:/EvolutionaryTradingAlgo/eta_engine/feeds/strategy_lab/app.py' not in strategy_lab_app
    assert 'C:/EvolutionaryTradingAlgo/data' not in strategy_lab_app
    assert 'C:/EvolutionaryTradingAlgo/reports/lab_reports' not in strategy_lab_app

    strategy_lab_run_batch = _read("eta_engine/feeds/strategy_lab/run_batch.py")
    assert 'workspace_roots.WORKSPACE_ROOT / "data"' in strategy_lab_run_batch
    assert 'workspace_roots.WORKSPACE_ROOT / "reports" / "lab_reports"' in strategy_lab_run_batch
    assert 'Path("C:/EvolutionaryTradingAlgo/data")' not in strategy_lab_run_batch
    assert 'Path("C:/EvolutionaryTradingAlgo/reports/lab_reports")' not in strategy_lab_run_batch


def test_firm_board_entrypoints_use_workspace_root_helpers() -> None:
    engage_firm_board_script = _read("eta_engine/scripts/engage_firm_board.py")
    assert 'workspace_roots.WORKSPACE_ROOT / "firm" / "the_firm_complete"' in engage_firm_board_script
    assert "workspace_roots.ETA_FIRM_BOARD_TEMP_SPEC_PATH" in engage_firm_board_script
    assert "workspace_roots.ETA_KILL_LOG_PATH" in engage_firm_board_script
    assert "workspace_roots.default_kill_log_path()" in engage_firm_board_script
    assert 'Path("C:/EvolutionaryTradingAlgo/firm/the_firm_complete")' not in engage_firm_board_script
    assert 'ROOT / "docs" / "_firm_spec_temp.json"' not in engage_firm_board_script
    assert 'ROOT / "docs" / "kill_log.json"' not in engage_firm_board_script

    engage_firm_board_feed = _read("eta_engine/feeds/engage_firm_board.py")
    assert "eta_engine.scripts.engage_firm_board" in engage_firm_board_feed
    assert "Compatibility shim" in engage_firm_board_feed

    live_tiny_preflight = _read("eta_engine/scripts/live_tiny_preflight_dryrun.py")
    assert "workspace_roots.ETA_PREFLIGHT_DRYRUN_DIR" in live_tiny_preflight
    assert "workspace_roots.default_paper_run_report_path()" in live_tiny_preflight
    assert "workspace_roots.default_decisions_v1_path()" in live_tiny_preflight
    assert "workspace_roots.default_kill_log_path()" in live_tiny_preflight
    assert "var/eta_engine/state/preflight/preflight_dryrun_report.json" in live_tiny_preflight
    assert "canonical paper-run report Tier-A PASS" in live_tiny_preflight
    assert "canonical decisions_v1 lock exists + complete" in live_tiny_preflight
    assert 'ROOT / "docs" / "kill_log.json"' not in live_tiny_preflight
    assert 'default=ROOT / "docs"' not in live_tiny_preflight
    assert 'ROOT / "docs" / "paper_run_report.json"' not in live_tiny_preflight
    assert 'ROOT / "docs" / "decisions_v1.json"' not in live_tiny_preflight

    live_tiny_preflight_feed = _read("eta_engine/feeds/live_tiny_preflight_dryrun.py")
    assert "eta_engine.scripts.live_tiny_preflight_dryrun" in live_tiny_preflight_feed
    assert "Compatibility shim" in live_tiny_preflight_feed

    go_trigger_script = _read("eta_engine/scripts/go_trigger.py")
    assert "workspace_roots.default_preflight_dryrun_report_path()" in go_trigger_script
    assert "workspace_roots.ETA_GO_TRIGGER_LOG_PATH" in go_trigger_script
    assert "var/eta_engine/state/go_trigger_log.jsonl" in go_trigger_script
    assert 'ROOT / "docs" / "preflight_dryrun_report.json"' not in go_trigger_script
    assert 'ROOT / "docs" / "go_trigger_log.jsonl"' not in go_trigger_script

    go_trigger_feed = _read("eta_engine/feeds/go_trigger.py")
    assert "eta_engine.scripts.go_trigger" in go_trigger_feed
    assert "Compatibility shim" in go_trigger_feed

    operator_action_queue = _read("eta_engine/scripts/operator_action_queue.py")
    assert "workspace_roots.default_preflight_dryrun_report_path()" in operator_action_queue
    assert 'ROOT / "docs" / "preflight_dryrun_report.json"' not in operator_action_queue

    backup_state = _read("eta_engine/scripts/_backup_state.py")
    assert "default_kill_log_path" in backup_state
    assert "default_decisions_v1_path" in backup_state
    assert "default_sharpe_baseline_path" in backup_state
    assert 'ROOT / "docs" / "kill_log.json"' not in backup_state
    assert 'ROOT / "docs" / "decisions_v1.json"' not in backup_state
    assert 'ROOT / "docs" / "sharpe_baseline.json"' not in backup_state

    backup_state_feed = _read("eta_engine/feeds/_backup_state.py")
    assert "eta_engine.scripts._backup_state" in backup_state_feed
    assert "Compatibility shim" in backup_state_feed


def test_btc_deploy_helpers_use_workspace_root_helpers() -> None:
    diag_fast = _read("eta_engine/deploy/scripts/diag_fast.py")
    assert "workspace_roots.ETA_RUNTIME_LOG_DIR" in diag_fast
    assert "workspace_roots.ETA_BROKER_ROUTER_PENDING_DIR" in diag_fast
    assert "workspace_roots.ETA_IBKR_BRIDGE_LOG_PATH" in diag_fast
    assert "workspace_roots.ETA_JARVIS_LIVE_HEALTH_PATH" in diag_fast
    assert 'Path("C:/EvolutionaryTradingAlgo/eta_engine/var/logs")' not in diag_fast
    assert 'Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")' not in diag_fast
    assert 'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/logs/ibkr_bridge.log")' not in diag_fast
    assert 'Path("C:/EvolutionaryTradingAlgo/var/eta_engine/state/jarvis_live_health.json")' not in diag_fast

    ibkr_order_bridge = _read("eta_engine/deploy/scripts/ibkr_order_bridge.py")
    assert (
        'os.environ.get("ETA_BROKER_ROUTER_PENDING_DIR", '
        'str(workspace_roots.ETA_BROKER_ROUTER_PENDING_DIR))'
    ) in ibkr_order_bridge
    assert "await venue._ensure_connected()" in ibkr_order_bridge
    assert 'Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")' not in ibkr_order_bridge

    test_exec_path = _read("eta_engine/deploy/scripts/test_exec_path.py")
    assert "workspace_roots.ETA_BROKER_ROUTER_PENDING_DIR" in test_exec_path
    assert 'Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")' not in test_exec_path

    test_submit = _read("eta_engine/deploy/scripts/test_submit.py")
    assert "bf_dir = cfg.broker_router_pending_dir" in test_submit
    assert 'Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")' not in test_submit

    write_test_order = _read("eta_engine/deploy/scripts/write_test_order.py")
    assert "workspace_roots.ETA_BROKER_ROUTER_PENDING_DIR" in write_test_order
    assert 'Path("C:/EvolutionaryTradingAlgo/eta_engine/docs/btc_live/broker_fleet")' not in write_test_order


def test_operator_metadata_uses_canonical_workspace_helpers() -> None:
    jarvis_actions = _read("eta_engine/common/jarvis/actions.py")
    assert "workspace_roots.ETA_ENGINE_ROOT" in jarvis_actions
    assert r"C:\EvolutionaryTradingAlgo\firm_command_center\eta_engine" not in jarvis_actions

    codex_overnight_operator = _read("eta_engine/scripts/codex_overnight_operator.py")
    assert '"canonical_write_root": str(WORKSPACE_ROOT)' in codex_overnight_operator
    assert '"canonical_write_root": "C:/EvolutionaryTradingAlgo"' not in codex_overnight_operator
