r"""Canonical ETA workspace paths for script defaults.

These helpers keep research and ops scripts anchored under the single
workspace root instead of legacy external data roots or per-user app paths.
"""

from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ETA_ENGINE_ROOT = Path(__file__).resolve().parents[1]

MNQ_DATA_ROOT = WORKSPACE_ROOT / "mnq_data"
MNQ_HISTORY_ROOT = MNQ_DATA_ROOT / "history"

CRYPTO_DATA_ROOT = WORKSPACE_ROOT / "data" / "crypto"
CRYPTO_HISTORY_ROOT = CRYPTO_DATA_ROOT / "history"
CRYPTO_IBKR_HISTORY_ROOT = CRYPTO_DATA_ROOT / "ibkr" / "history"
CRYPTO_ONCHAIN_ROOT = CRYPTO_DATA_ROOT / "onchain"
CRYPTO_SENTIMENT_ROOT = CRYPTO_DATA_ROOT / "sentiment"
CRYPTO_MACRO_ROOT = CRYPTO_DATA_ROOT / "macro"

BACKTEST_CACHE_ROOT = WORKSPACE_ROOT / "mnq_backtest" / ".cache" / "parquet"
BACKTEST_RUNS_ROOT = WORKSPACE_ROOT / "mnq_backtest" / "runs"
BACKTEST_DATA_ROOT = WORKSPACE_ROOT / "mnq_backtest" / "data"
DATABENTO_DATA_ROOT = WORKSPACE_ROOT / "data" / "bars" / "databento"

ROOT_LOGS_DIR = WORKSPACE_ROOT / "logs"
ROOT_VAR_DIR = WORKSPACE_ROOT / "var"
ETA_RUNTIME_STATE_DIR = ROOT_VAR_DIR / "eta_engine" / "state"
ETA_RUNTIME_LOG_DIR = ROOT_LOGS_DIR / "eta_engine"
ETA_RUNTIME_HEALTH_DIR = ETA_RUNTIME_STATE_DIR / "health"
ETA_DATA_LAKE_ROOT = ROOT_VAR_DIR / "eta_engine" / "data_lake"
ETA_BOT_STATE_ROOT = ETA_RUNTIME_STATE_DIR
ETA_EVENT_CALENDAR_PATH = ETA_RUNTIME_STATE_DIR / "event_calendar.yaml"
ETA_FM_TRADE_GATES_LOG_PATH = ETA_RUNTIME_STATE_DIR / "fm_trade_gates.jsonl"
ETA_IDEMPOTENCY_STORE_PATH = ETA_RUNTIME_STATE_DIR / "idempotency.jsonl"
ETA_KAIZEN_LEDGER_PATH = ETA_RUNTIME_STATE_DIR / "kaizen_ledger.json"
ETA_KAIZEN_LEDGER_JSONL_PATH = ETA_RUNTIME_STATE_DIR / "kaizen_ledger.jsonl"
ETA_KAIZEN_REPORT_DIR = ETA_RUNTIME_STATE_DIR / "kaizen_reports"
ETA_KAIZEN_ACTIONS_LOG_PATH = ETA_RUNTIME_STATE_DIR / "kaizen_actions.jsonl"
ETA_KAIZEN_OVERRIDES_PATH = ETA_RUNTIME_STATE_DIR / "kaizen_overrides.json"
ETA_KAIZEN_REACTIVATE_LOG_PATH = ETA_RUNTIME_STATE_DIR / "kaizen_reactivate.log"
ETA_PAPER_SOAK_LEDGER_PATH = ETA_RUNTIME_STATE_DIR / "paper_soak_ledger.json"
ETA_CAPITAL_ALLOCATION_PATH = ETA_RUNTIME_STATE_DIR / "capital_allocation.json"
ETA_PROP_FIRM_ACCOUNT_MAP_PATH = ETA_RUNTIME_STATE_DIR / "prop_firm_accounts.json"
ETA_INVESTOR_DASHBOARD_PATH = ROOT_VAR_DIR / "eta_engine" / "investor_dashboard" / "index.html"
ETA_NOTION_EXPORT_DIR = ROOT_VAR_DIR / "eta_engine" / "notion_export"
ETA_JARVIS_AUDIT_DIR = ETA_RUNTIME_STATE_DIR / "jarvis_audit"
ETA_KAIZEN_CRITIQUE_DIR = ETA_RUNTIME_STATE_DIR / "kaizen_critique"
ETA_BANDIT_PROMOTION_DIR = ETA_RUNTIME_STATE_DIR / "bandit"
ETA_MODEL_ARTIFACT_DIR = ETA_RUNTIME_STATE_DIR / "models"
ETA_CORRELATION_ARTIFACT_DIR = ETA_RUNTIME_STATE_DIR / "correlation"
ETA_CORRELATION_REGIME_DIR = ETA_RUNTIME_STATE_DIR / "correlation_regime"
ETA_ANOMALY_ALERT_STATE_PATH = ETA_RUNTIME_STATE_DIR / "anomaly" / "last_alert.json"
ETA_CALIBRATION_MODEL_PATH = ETA_RUNTIME_STATE_DIR / "calibration" / "platt_sigmoid.json"
ETA_CALIBRATOR_LABELS_PATH = ETA_RUNTIME_STATE_DIR / "calibrator_labels.jsonl"
ETA_HOT_LEARNER_STATE_PATH = ETA_RUNTIME_STATE_DIR / "hot_learner.json"
ETA_FLEET_STATE_PATH = ETA_RUNTIME_STATE_DIR / "fleet_state.json"
ETA_JARVIS_TRACE_PATH = ETA_RUNTIME_STATE_DIR / "jarvis_trace.jsonl"
ETA_JARVIS_WIRING_AUDIT_PATH = ETA_RUNTIME_STATE_DIR / "jarvis_wiring_audit.json"
ETA_KAIZEN_LATEST_PATH = ETA_RUNTIME_STATE_DIR / "kaizen_latest.json"
ETA_REGIME_STATE_PATH = ETA_RUNTIME_STATE_DIR / "regime_state.json"
ETA_AGENT_REGISTRY_PATH = ETA_RUNTIME_STATE_DIR / "agent_registry.json"
ETA_HERMES_OVERRIDES_PATH = ETA_RUNTIME_STATE_DIR / "hermes_overrides.json"
ETA_HERMES_ACTIONS_LOG_PATH = ETA_RUNTIME_STATE_DIR / "hermes_actions.jsonl"
ETA_HERMES_MEMORY_DB_PATH = ETA_RUNTIME_STATE_DIR / "hermes_memory_store.db"
ETA_HERMES_MEMORY_BACKUP_DIR = ETA_RUNTIME_STATE_DIR / "backups" / "hermes_memory"
ETA_SENTIMENT_CACHE_DIR = ETA_RUNTIME_STATE_DIR / "sentiment"
ETA_TRADE_JOURNAL_DIR = ETA_RUNTIME_STATE_DIR / "trade_journal"
ETA_VERDICT_WEBHOOK_CURSOR_PATH = ETA_RUNTIME_STATE_DIR / "verdict_webhook" / "cursor.json"
ETA_JARVIS_DENIAL_RATE_ALERT_STATE_PATH = ETA_RUNTIME_STATE_DIR / "jarvis_denial_rate_state.json"
ETA_RUNTIME_DECISION_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "decision_journal.jsonl"
ETA_QUANTUM_STATE_DIR = ETA_RUNTIME_STATE_DIR / "quantum"
ETA_QUANTUM_CURRENT_ALLOCATION_PATH = ETA_QUANTUM_STATE_DIR / "current_allocation.json"
ETA_QUANTUM_JOBS_LOG_PATH = ETA_QUANTUM_STATE_DIR / "jobs.jsonl"
ETA_QUANTUM_RESULT_CACHE_PATH = ETA_QUANTUM_STATE_DIR / "result_cache.json"
ETA_DATA_INVENTORY_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "data_inventory_latest.json"
ETA_DIAMOND_LEADERBOARD_PATH = ETA_RUNTIME_STATE_DIR / "diamond_leaderboard_latest.json"
ETA_DIAMOND_PROP_LAUNCH_READINESS_PATH = ETA_RUNTIME_STATE_DIR / "diamond_prop_launch_readiness_latest.json"
ETA_DIAMOND_WATCHDOG_PATH = ETA_RUNTIME_STATE_DIR / "diamond_watchdog_latest.json"
ETA_DIAMOND_DEMOTION_GATE_PATH = ETA_RUNTIME_STATE_DIR / "diamond_demotion_gate_latest.json"
ETA_DIAMOND_DIRECTION_STRATIFY_PATH = ETA_RUNTIME_STATE_DIR / "diamond_direction_stratify_latest.json"
ETA_DIAMOND_FEED_SANITY_AUDIT_PATH = ETA_RUNTIME_STATE_DIR / "diamond_feed_sanity_audit_latest.json"
ETA_DIAMOND_PROMOTION_GATE_PATH = ETA_RUNTIME_STATE_DIR / "diamond_promotion_gate_latest.json"
ETA_DIAMOND_SIZING_AUDIT_PATH = ETA_RUNTIME_STATE_DIR / "diamond_sizing_audit_latest.json"
ETA_DIAMOND_OPS_DASHBOARD_PATH = ETA_RUNTIME_STATE_DIR / "diamond_ops_dashboard_latest.json"
ETA_DIAMOND_PROP_ALLOCATOR_PATH = ETA_RUNTIME_STATE_DIR / "diamond_prop_allocator_latest.json"
ETA_DIAMOND_PROP_DRAWDOWN_GUARD_PATH = ETA_RUNTIME_STATE_DIR / "diamond_prop_drawdown_guard_latest.json"
ETA_DIAMOND_PROP_ALERT_CURSOR_PATH = ETA_RUNTIME_STATE_DIR / "diamond_prop_alert_cursor.json"
ETA_DIAMOND_PROP_ALERT_DISPATCHER_PATH = ETA_RUNTIME_STATE_DIR / "diamond_prop_alert_dispatcher_latest.json"
ETA_DIAMOND_QTY_ASYMMETRY_PATH = ETA_RUNTIME_STATE_DIR / "diamond_qty_asymmetry_latest.json"
ETA_DIAMOND_LIVE_PAPER_DRIFT_PATH = ETA_RUNTIME_STATE_DIR / "diamond_live_paper_drift_latest.json"
ETA_DIAMOND_PRESET_VALIDATION_PATH = ETA_RUNTIME_STATE_DIR / "diamond_preset_validation_latest.json"
ETA_DIAMOND_AUTHENTICITY_PATH = ETA_RUNTIME_STATE_DIR / "diamond_authenticity_latest.json"
ETA_DIAMOND_PROP_PRELAUNCH_DRYRUN_PATH = ETA_RUNTIME_STATE_DIR / "diamond_prop_prelaunch_dryrun_latest.json"
ETA_DIAMOND_WAVE25_STATUS_PATH = ETA_RUNTIME_STATE_DIR / "diamond_wave25_status_latest.json"
ETA_DIAMOND_RETUNE_STATUS_PATH = ETA_RUNTIME_STATE_DIR / "diamond_retune_status_latest.json"
ETA_DIAMOND_CPCV_PATH = ETA_RUNTIME_STATE_DIR / "diamond_cpcv_latest.json"
ETA_DIAMOND_SANITIZER_PATH = ETA_RUNTIME_STATE_DIR / "diamond_sanitizer_latest.json"
ETA_DIAMOND_REGIME_STRATIFY_PATH = ETA_RUNTIME_STATE_DIR / "diamond_regime_stratify_latest.json"
ETA_PROP_HALT_FLAG_PATH = ETA_RUNTIME_STATE_DIR / "prop_halt_active.flag"
ETA_PROP_WATCH_FLAG_PATH = ETA_RUNTIME_STATE_DIR / "prop_watch_active.flag"
ETA_PUBLIC_BROKER_CLOSE_TRUTH_CACHE_PATH = ETA_RUNTIME_HEALTH_DIR / "public_broker_close_truth_latest.json"
ETA_L2_BACKTEST_RUNS_LOG_PATH = ETA_RUNTIME_LOG_DIR / "l2_backtest_runs.jsonl"
ETA_DIAMOND_AUTHENTICITY_LOG_PATH = ETA_RUNTIME_LOG_DIR / "diamond_authenticity.jsonl"
ETA_SYMBOL_INTELLIGENCE_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "symbol_intelligence_latest.json"
ETA_SYMBOL_INTELLIGENCE_COLLECTOR_STATUS_PATH = ETA_RUNTIME_STATE_DIR / "symbol_intelligence_collector_latest.json"
ETA_SYMBOL_INTELLIGENCE_COLLECTOR_LOCK_PATH = ETA_RUNTIME_STATE_DIR / "symbol_intelligence_collector.lock"
ETA_INDEX_FUTURES_BAR_REFRESH_STATUS_PATH = ETA_RUNTIME_STATE_DIR / "index_futures_bar_refresh_latest.json"
ETA_JARVIS_V3_EVENTS_PATH = ETA_RUNTIME_STATE_DIR / "jarvis_v3_events.jsonl"
ETA_DASHBOARD_EVENTS_PATH = ETA_RUNTIME_STATE_DIR / "dashboard_events.jsonl"
ETA_ETA_EVENTS_LOG_PATH = ETA_RUNTIME_STATE_DIR / "eta_events.jsonl"
ETA_UPTIME_EVENTS_PATH = ETA_RUNTIME_STATE_DIR / "uptime_events.jsonl"
ETA_ETA_ALERT_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "eta_alert_snapshot.json"
ETA_TWS_WATCHDOG_STATUS_PATH = ETA_RUNTIME_STATE_DIR / "tws_watchdog.json"
ETA_CUTOVER_STATUS_PATH = ETA_RUNTIME_STATE_DIR / "cutover_status.json"
ETA_MULTI_MODEL_TELEMETRY_LOG_PATH = ETA_RUNTIME_STATE_DIR / "multi_model_telemetry.jsonl"
ETA_ANOMALY_HITS_LOG_PATH = ETA_RUNTIME_STATE_DIR / "anomaly_watcher.jsonl"
ETA_TELEGRAM_INBOUND_OFFSET_PATH = ETA_RUNTIME_STATE_DIR / "telegram_inbound_offset.json"
ETA_TELEGRAM_SILENCE_UNTIL_PATH = ETA_RUNTIME_STATE_DIR / "telegram_silence_until.json"
ETA_TELEGRAM_HERMES_LAST_CHAT_PATH = ETA_RUNTIME_STATE_DIR / "telegram_hermes_last_chat.json"
ETA_HERMES_PROACTIVE_CURSOR_PATH = ETA_RUNTIME_STATE_DIR / "hermes_proactive_cursor.json"
ETA_RESEARCH_GRID_RUNTIME_DIR = ETA_RUNTIME_STATE_DIR / "research_grid"
ETA_LIVE_DATA_RUNTIME_DIR = ETA_RUNTIME_STATE_DIR / "live_data"
ETA_TRADINGVIEW_AUTH_STATE_PATH = ETA_RUNTIME_STATE_DIR / "tradingview_auth.json"
ETA_TRADINGVIEW_DATA_ROOT = ETA_LIVE_DATA_RUNTIME_DIR / "tradingview"
ETA_OPERATOR_QUEUE_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "operator_queue_snapshot.json"
ETA_OPERATOR_QUEUE_PREVIOUS_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "operator_queue_snapshot.previous.json"
ETA_DIRTY_WORKTREE_RECONCILIATION_PATH = ETA_RUNTIME_STATE_DIR / "dirty_worktree_reconciliation_latest.json"
ETA_IBC_CUTOVER_READINESS_PATH = ETA_RUNTIME_STATE_DIR / "ibc_cutover_readiness.json"
ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "bot_strategy_readiness_latest.json"
ETA_PAPER_LIVE_LAUNCH_CHECK_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "paper_live_launch_check_latest.json"
ETA_STRATEGY_SUPERCHARGE_SCORECARD_PATH = ETA_RUNTIME_STATE_DIR / "strategy_supercharge_scorecard_latest.json"
ETA_STRATEGY_SUPERCHARGE_MANIFEST_PATH = ETA_RUNTIME_STATE_DIR / "strategy_supercharge_manifest_latest.json"
ETA_STRATEGY_SUPERCHARGE_RESULTS_PATH = ETA_RUNTIME_STATE_DIR / "strategy_supercharge_results_latest.json"
ETA_JARVIS_INTEL_STATE_DIR = ETA_RUNTIME_STATE_DIR / "jarvis_intel"
ETA_JARVIS_DAILY_BRIEF_DIR = ETA_JARVIS_INTEL_STATE_DIR / "daily_briefs"
ETA_JARVIS_POSTMORTEM_DIR = ETA_JARVIS_INTEL_STATE_DIR / "postmortems"
ETA_HERMES_STATE_PATH = ETA_JARVIS_INTEL_STATE_DIR / "hermes_state.json"
ETA_JARVIS_SUPERVISOR_STATE_DIR = ETA_JARVIS_INTEL_STATE_DIR / "supervisor"
ETA_JARVIS_SUPERVISOR_HEARTBEAT_PATH = ETA_JARVIS_SUPERVISOR_STATE_DIR / "heartbeat.json"
# Independent keep-alive stamp: a daemon thread inside the supervisor
# refreshes this file every KEEPALIVE_PERIOD_S seconds, even when the
# main tick loop is blocked. Combined with the main heartbeat, the
# diagnostic CLI can distinguish "process stuck" from "process dead".
ETA_JARVIS_SUPERVISOR_KEEPALIVE_PATH = ETA_JARVIS_SUPERVISOR_STATE_DIR / "heartbeat_keepalive.json"
ETA_JARVIS_SUPERVISOR_HEARTBEAT_ERRORS_PATH = ETA_JARVIS_SUPERVISOR_STATE_DIR / "heartbeat_write_errors.jsonl"
ETA_JARVIS_SUPERVISOR_RECONCILE_PATH = ETA_JARVIS_SUPERVISOR_STATE_DIR / "reconcile_last.json"
ETA_ORDER_ENTRY_HOLD_PATH = ETA_RUNTIME_STATE_DIR / "order_entry_hold.json"
ETA_JARVIS_TRADE_CLOSES_PATH = ETA_JARVIS_INTEL_STATE_DIR / "trade_closes.jsonl"
ETA_JARVIS_RISK_BUDGET_SNAPSHOT_PATH = ETA_JARVIS_INTEL_STATE_DIR / "risk_budget_snapshot.json"
ETA_JARVIS_SHADOW_SIGNALS_PATH = ETA_JARVIS_INTEL_STATE_DIR / "shadow_signals.jsonl"
ETA_JARVIS_SHADOW_SIGNAL_OUTCOMES_PATH = ETA_JARVIS_INTEL_STATE_DIR / "shadow_signal_outcomes_latest.json"
ETA_CLOSED_TRADE_LEDGER_PATH = ETA_RUNTIME_STATE_DIR / "closed_trade_ledger_latest.json"
ETA_BROKER_BRACKET_AUDIT_PATH = ETA_RUNTIME_STATE_DIR / "broker_bracket_audit_latest.json"
ETA_BROKER_BRACKET_MANUAL_ACK_PATH = ETA_RUNTIME_STATE_DIR / "broker_bracket_manual_oco_ack.json"
ETA_BROKER_ROUTER_FILLS_PATH = ETA_RUNTIME_STATE_DIR / "broker_router_fills.jsonl"
ETA_BROKER_ROUTER_STATE_DIR = ETA_RUNTIME_STATE_DIR / "router"
ETA_BROKER_ROUTER_PENDING_DIR = ETA_BROKER_ROUTER_STATE_DIR / "pending"
ETA_BROKER_ROUTER_PROCESSED_DIR = ETA_BROKER_ROUTER_STATE_DIR / "processed"
ETA_BROKER_CONNECTION_REPORT_DIR = ETA_RUNTIME_STATE_DIR / "broker_connections"
ETA_PROP_OPERATOR_CHECKLIST_PATH = ETA_RUNTIME_STATE_DIR / "prop_operator_checklist_latest.json"
ETA_PROP_STRATEGY_PROMOTION_AUDIT_PATH = ETA_RUNTIME_STATE_DIR / "prop_strategy_promotion_audit_latest.json"
ETA_BOT_LIFECYCLE_STATE_PATH = ETA_RUNTIME_STATE_DIR / "bot_lifecycle.json"
ETA_VPS_OPS_HARDENING_AUDIT_PATH = ETA_RUNTIME_STATE_DIR / "vps_ops_hardening_latest.json"
ETA_DAILY_STOP_RESET_AUDIT_PATH = ETA_RUNTIME_STATE_DIR / "daily_stop_reset_audit_latest.json"
ETA_DRIFT_WATCHDOG_LOG_PATH = ETA_RUNTIME_STATE_DIR / "drift_watchdog.jsonl"
ETA_JARVIS_DRIFT_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "jarvis_drift.jsonl"
ETA_SHARED_BREAKER_STATE_PATH = ETA_RUNTIME_STATE_DIR / "breaker.json"
ETA_DEADMAN_SENTINEL_PATH = ETA_RUNTIME_STATE_DIR / "operator.sentinel"
ETA_DEADMAN_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "operator_activity.jsonl"
ETA_PROMOTION_STATE_PATH = ETA_RUNTIME_STATE_DIR / "promotion.json"
ETA_PROMOTION_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "promotion.jsonl"
ETA_AVENGERS_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "avengers.jsonl"
ETA_CALIBRATION_JOURNAL_PATH = ETA_RUNTIME_STATE_DIR / "calibration.jsonl"
ETA_AVENGER_DAEMON_PID_DIR = ETA_RUNTIME_STATE_DIR / "avenger_daemons"
# B-class migrators: catastrophic-verdict latch + tick-granular trailing
# DD tracker. Legacy in-repo paths are kept only as read fallbacks during
# the migration window so that a runtime starting up after the cutover
# finds prior persisted state. New writes always land at the canonical
# workspace path below per CLAUDE.md hard rule #1.  # HISTORICAL-PATH-OK
ETA_KILL_SWITCH_LATCH_PATH = ETA_RUNTIME_STATE_DIR / "kill_switch_latch.json"
ETA_TRAILING_DD_TRACKER_PATH = ETA_RUNTIME_STATE_DIR / "trailing_dd_tracker.json"
ETA_LEGACY_KILL_SWITCH_LATCH_PATH = ETA_ENGINE_ROOT / "state" / "kill_switch_latch.json"
ETA_LEGACY_TRAILING_DD_TRACKER_PATH = ETA_ENGINE_ROOT / "state" / "trailing_dd_tracker.json"
# Force-Multiplier health probe snapshot. Task Scheduler writes a
# JSON snapshot every 15 minutes that dashboards poll; the canonical write target
# is under var/, with the in-repo state/ kept as a one-shot read fallback
# during the migration window.
ETA_FM_HEALTH_SNAPSHOT_PATH = ETA_RUNTIME_STATE_DIR / "fm_health.json"
ETA_LEGACY_FM_HEALTH_SNAPSHOT_PATH = ETA_ENGINE_ROOT / "state" / "fm_health.json"
# JARVIS verdict log read by the read-only inspection scripts and several
# v2* policies. The canonical write path is under var/; the legacy
# in-repo path is kept as a read fallback so existing logs remain
# inspectable until the next session rolls a fresh log.
ETA_JARVIS_VERDICTS_PATH = ETA_JARVIS_INTEL_STATE_DIR / "verdicts.jsonl"
ETA_LEGACY_JARVIS_INTEL_STATE_DIR = ETA_ENGINE_ROOT / "state" / "jarvis_intel"
ETA_LEGACY_JARVIS_DAILY_BRIEF_DIR = ETA_LEGACY_JARVIS_INTEL_STATE_DIR / "daily_briefs"
ETA_LEGACY_JARVIS_POSTMORTEM_DIR = ETA_LEGACY_JARVIS_INTEL_STATE_DIR / "postmortems"
ETA_LEGACY_JARVIS_VERDICTS_PATH = ETA_LEGACY_JARVIS_INTEL_STATE_DIR / "verdicts.jsonl"
ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH = ETA_LEGACY_JARVIS_INTEL_STATE_DIR / "trade_closes.jsonl"
ETA_LEGACY_JARVIS_AUDIT_DIR = ETA_ENGINE_ROOT / "state" / "jarvis_audit"
ETA_LEGACY_KAIZEN_CRITIQUE_DIR = ETA_ENGINE_ROOT / "state" / "kaizen_critique"
ETA_LEGACY_BANDIT_PROMOTION_DIR = ETA_ENGINE_ROOT / "state" / "bandit"
ETA_LEGACY_MODEL_ARTIFACT_DIR = ETA_ENGINE_ROOT / "state" / "models"
ETA_LEGACY_CORRELATION_ARTIFACT_DIR = ETA_ENGINE_ROOT / "state" / "correlation"
ETA_LEGACY_CORRELATION_REGIME_DIR = ETA_ENGINE_ROOT / "state" / "correlation_regime"
ETA_LEGACY_ANOMALY_ALERT_STATE_PATH = ETA_ENGINE_ROOT / "state" / "anomaly" / "last_alert.json"
ETA_LEGACY_VERDICT_WEBHOOK_CURSOR_PATH = ETA_ENGINE_ROOT / "state" / "verdict_webhook" / "cursor.json"
ETA_LEGACY_JARVIS_DENIAL_RATE_ALERT_STATE_PATH = ETA_ENGINE_ROOT / "var" / "alerter" / "jarvis_denial_rate_state.json"
ETA_LEGACY_QUANTUM_STATE_DIR = ETA_ENGINE_ROOT / "state" / "quantum"
ETA_LEGACY_QUANTUM_CURRENT_ALLOCATION_PATH = ETA_LEGACY_QUANTUM_STATE_DIR / "current_allocation.json"
ETA_LEGACY_QUANTUM_JOBS_LOG_PATH = ETA_LEGACY_QUANTUM_STATE_DIR / "jobs.jsonl"
ETA_LEGACY_QUANTUM_RESULT_CACHE_PATH = ETA_LEGACY_QUANTUM_STATE_DIR / "result_cache.json"
# Eval results: promptfoo writes a single JSON aggregate per run.
ETA_EVAL_PROMPTFOO_RESULTS_PATH = ETA_RUNTIME_STATE_DIR / "eval" / "promptfoo_results.json"
ETA_LEGACY_EVAL_PROMPTFOO_RESULTS_PATH = ETA_ENGINE_ROOT / "state" / "eval" / "promptfoo_results.json"
# Hermes-bridge `/kill confirm` latch: the consolidated single canonical
# write target. The bridge previously fanned out to three paths; the
# canonical/legacy split below replaces the multi-target latch.
ETA_HERMES_KILL_LATCH_PATH = ETA_RUNTIME_STATE_DIR / "kill_switch_latch.json"
ETA_LEGACY_HERMES_KILL_LATCH_PATH = ETA_ENGINE_ROOT / "state" / "kill_switch_latch.json"
ETA_RUNTIME_ALERTS_LOG_PATH = ETA_RUNTIME_LOG_DIR / "alerts_log.jsonl"
ETA_RUNTIME_LOG_PATH = ETA_RUNTIME_LOG_DIR / "runtime_log.jsonl"
ETA_IBKR_BRIDGE_LOG_PATH = ETA_RUNTIME_LOG_DIR / "ibkr_bridge.log"
ETA_AVENGER_METRICS_PATH = ETA_RUNTIME_LOG_DIR / "metrics.prom"
ETA_DAILY_DEBRIEF_LOG_PATH = ETA_RUNTIME_LOG_DIR / "daily_debrief.jsonl"
ETA_PREFLIGHT_RUNS_LOG_PATH = ETA_RUNTIME_LOG_DIR / "preflight_runs.jsonl"
ETA_ANOMALY_PULSE_LOG_PATH = ETA_RUNTIME_LOG_DIR / "anomaly_pulse.jsonl"
ETA_TELEGRAM_INBOUND_AUDIT_LOG_PATH = ETA_RUNTIME_LOG_DIR / "telegram_inbound.jsonl"
ETA_HERMES_PROACTIVE_AUDIT_PATH = ETA_RUNTIME_LOG_DIR / "hermes_proactive_audit.jsonl"
ETA_HERMES_VOICE_LOG_PATH = ETA_RUNTIME_LOG_DIR / "hermes_voice.log"
ETA_JARVIS_LIVE_HEALTH_PATH = ETA_RUNTIME_STATE_DIR / "jarvis_live_health.json"
ETA_JARVIS_LIVE_LOG_PATH = ETA_RUNTIME_STATE_DIR / "jarvis_live_log.jsonl"
ETA_BTC_PAPER_STATE_DIR = ETA_RUNTIME_STATE_DIR / "btc_paper"
ETA_BTC_PAPER_RUN_LATEST_PATH = ETA_BTC_PAPER_STATE_DIR / "btc_paper_run_latest.json"
ETA_BTC_LIVE_STATE_DIR = ETA_RUNTIME_STATE_DIR / "btc_live"
ETA_BTC_LIVE_DECISIONS_PATH = ETA_BTC_LIVE_STATE_DIR / "btc_live_decisions.jsonl"
ETA_BTC_BROKER_FLEET_STATE_DIR = ETA_RUNTIME_STATE_DIR / "broker_fleet"
ETA_MNQ_LIVE_STATE_DIR = ETA_RUNTIME_STATE_DIR / "mnq_live"
ETA_INTEGRATIONS_REPORT_DIR = ETA_RUNTIME_STATE_DIR / "integrations"
ETA_INTEGRATIONS_LIVE_STATUS_PATH = ETA_INTEGRATIONS_REPORT_DIR / "integrations_live_status.json"
ETA_MONTHLY_REVIEW_DIR = ETA_RUNTIME_STATE_DIR / "monthly_review"
ETA_WEEKLY_REVIEW_DIR = ETA_RUNTIME_STATE_DIR / "weekly_review"
ETA_FIRM_BOARD_STATE_DIR = ETA_RUNTIME_STATE_DIR / "firm_board"
ETA_FIRM_BOARD_TEMP_SPEC_PATH = ETA_FIRM_BOARD_STATE_DIR / "_firm_spec_temp.json"
ETA_KILL_LOG_PATH = ETA_RUNTIME_STATE_DIR / "kill_log.json"
ETA_WEEKLY_REVIEW_LOG_PATH = ETA_WEEKLY_REVIEW_DIR / "weekly_review_log.json"
ETA_WEEKLY_REVIEW_LATEST_JSON_PATH = ETA_WEEKLY_REVIEW_DIR / "weekly_review_latest.json"
ETA_WEEKLY_REVIEW_LATEST_TXT_PATH = ETA_WEEKLY_REVIEW_DIR / "weekly_review_latest.txt"
ETA_WEEKLY_CHECKLIST_TEMPLATE_PATH = ETA_WEEKLY_REVIEW_DIR / "weekly_checklist_template.json"
ETA_WEEKLY_CHECKLIST_LATEST_JSON_PATH = ETA_WEEKLY_REVIEW_DIR / "weekly_checklist_latest.json"
ETA_WEEKLY_CHECKLIST_LATEST_TXT_PATH = ETA_WEEKLY_REVIEW_DIR / "weekly_checklist_latest.txt"
ETA_PREMARKET_INPUTS_PATH = ETA_RUNTIME_STATE_DIR / "premarket_inputs.json"
ETA_PREMARKET_REPORT_DIR = ETA_RUNTIME_STATE_DIR / "premarket"
ETA_PAPER_RUN_DIR = ETA_RUNTIME_STATE_DIR / "paper_run"
ETA_PAPER_RUN_REPORT_PATH = ETA_PAPER_RUN_DIR / "paper_run_report.json"
ETA_PAPER_RUN_TEARSHEET_PATH = ETA_PAPER_RUN_DIR / "paper_run_tearsheet.txt"
ETA_PREFLIGHT_DRYRUN_DIR = ETA_RUNTIME_STATE_DIR / "preflight"
ETA_PREFLIGHT_DRYRUN_REPORT_PATH = ETA_PREFLIGHT_DRYRUN_DIR / "preflight_dryrun_report.json"
ETA_PREFLIGHT_DRYRUN_LOG_PATH = ETA_PREFLIGHT_DRYRUN_DIR / "preflight_dryrun_log.txt"
ETA_GO_TRIGGER_LOG_PATH = ETA_RUNTIME_STATE_DIR / "go_trigger_log.jsonl"
ETA_DECISIONS_V1_PATH = ETA_RUNTIME_STATE_DIR / "decisions_v1.json"
ETA_SHARPE_BASELINE_PATH = ETA_RUNTIME_STATE_DIR / "sharpe_baseline.json"
ETA_LEGACY_BROKER_CONNECTION_REPORT_DIR = ETA_ENGINE_ROOT / "docs" / "broker_connections"
ETA_LEGACY_MNQ_LIVE_STATE_DIR = ETA_ENGINE_ROOT / "docs" / "mnq_live"
ETA_LEGACY_INTEGRATIONS_REPORT_DIR = ETA_ENGINE_ROOT / "docs"
ETA_LEGACY_INTEGRATIONS_LIVE_STATUS_PATH = ETA_LEGACY_INTEGRATIONS_REPORT_DIR / "integrations_live_status.json"
ETA_LEGACY_MONTHLY_REVIEW_DIR = ETA_ENGINE_ROOT / "docs"
ETA_LEGACY_WEEKLY_REVIEW_DIR = ETA_ENGINE_ROOT / "docs"
ETA_LEGACY_KILL_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "kill_log.json"
ETA_LEGACY_WEEKLY_REVIEW_LOG_PATH = ETA_LEGACY_WEEKLY_REVIEW_DIR / "weekly_review_log.json"
ETA_LEGACY_WEEKLY_REVIEW_LATEST_JSON_PATH = ETA_LEGACY_WEEKLY_REVIEW_DIR / "weekly_review_latest.json"
ETA_LEGACY_WEEKLY_REVIEW_LATEST_TXT_PATH = ETA_LEGACY_WEEKLY_REVIEW_DIR / "weekly_review_latest.txt"
ETA_LEGACY_WEEKLY_CHECKLIST_TEMPLATE_PATH = ETA_LEGACY_WEEKLY_REVIEW_DIR / "weekly_checklist_template.json"
ETA_LEGACY_WEEKLY_CHECKLIST_LATEST_JSON_PATH = ETA_LEGACY_WEEKLY_REVIEW_DIR / "weekly_checklist_latest.json"
ETA_LEGACY_WEEKLY_CHECKLIST_LATEST_TXT_PATH = ETA_LEGACY_WEEKLY_REVIEW_DIR / "weekly_checklist_latest.txt"
ETA_LEGACY_PREMARKET_INPUTS_PATH = ETA_ENGINE_ROOT / "docs" / "premarket_inputs.json"
ETA_LEGACY_PREMARKET_REPORT_DIR = ETA_ENGINE_ROOT / "docs"
ETA_LEGACY_PAPER_RUN_DIR = ETA_ENGINE_ROOT / "docs"
ETA_LEGACY_PAPER_RUN_REPORT_PATH = ETA_LEGACY_PAPER_RUN_DIR / "paper_run_report.json"
ETA_LEGACY_PAPER_RUN_TEARSHEET_PATH = ETA_LEGACY_PAPER_RUN_DIR / "paper_run_tearsheet.txt"
ETA_LEGACY_PREFLIGHT_DRYRUN_REPORT_PATH = ETA_ENGINE_ROOT / "docs" / "preflight_dryrun_report.json"
ETA_LEGACY_PREFLIGHT_DRYRUN_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "preflight_dryrun_log.txt"
ETA_LEGACY_GO_TRIGGER_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "go_trigger_log.jsonl"
ETA_LEGACY_DECISIONS_V1_PATH = ETA_ENGINE_ROOT / "docs" / "decisions_v1.json"
ETA_LEGACY_SHARPE_BASELINE_PATH = ETA_ENGINE_ROOT / "docs" / "sharpe_baseline.json"
ETA_BRIDGE_AUTOHEAL_ACTIONS_LOG_PATH = ROOT_VAR_DIR / "bridge_autoheal_actions.jsonl"
ETA_HERMES_EVENING_JOURNAL_AUDIT_PATH = ROOT_VAR_DIR / "hermes_evening_journal.jsonl"
ETA_LEGACY_DOCS_DRIFT_WATCHDOG_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "drift_watchdog.jsonl"
ETA_LEGACY_DOCS_ALERTS_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "alerts_log.jsonl"
ETA_LEGACY_DOCS_RUNTIME_LOG_PATH = ETA_ENGINE_ROOT / "docs" / "runtime_log.jsonl"
ETA_LEGACY_BTC_LIVE_DECISIONS_PATH = ETA_ENGINE_ROOT / "docs" / "btc_live" / "btc_live_decisions.jsonl"
ETA_LEGACY_ANOMALY_HITS_LOG_PATH = ROOT_VAR_DIR / "anomaly_watcher.jsonl"
ETA_LEGACY_ANOMALY_PULSE_LOG_PATH = ROOT_VAR_DIR / "anomaly_pulse.jsonl"
ETA_LEGACY_TELEGRAM_INBOUND_OFFSET_PATH = ROOT_VAR_DIR / "telegram_inbound_offset.json"
ETA_LEGACY_TELEGRAM_SILENCE_UNTIL_PATH = ROOT_VAR_DIR / "telegram_silence_until.json"
ETA_LEGACY_TELEGRAM_HERMES_LAST_CHAT_PATH = ROOT_VAR_DIR / "telegram_hermes_last_chat.json"
ETA_LEGACY_TELEGRAM_INBOUND_LOG_PATH = ROOT_VAR_DIR / "telegram_inbound.log"
ETA_LEGACY_TELEGRAM_INBOUND_ERR_PATH = ROOT_VAR_DIR / "telegram_inbound.err"
ETA_LEGACY_TELEGRAM_INBOUND_AUDIT_LOG_PATH = ROOT_VAR_DIR / "telegram_inbound.jsonl"
ETA_LEGACY_HERMES_PROACTIVE_CURSOR_PATH = ROOT_VAR_DIR / "hermes_proactive_cursor.json"
ETA_LEGACY_HERMES_PROACTIVE_AUDIT_PATH = ROOT_VAR_DIR / "hermes_proactive_audit.jsonl"
ETA_LEGACY_HERMES_VOICE_LOG_PATH = ROOT_VAR_DIR / "hermes_voice.log"
ETA_LEGACY_JARVIS_DRIFT_JOURNAL_PATH = Path.home() / ".jarvis" / "drift.jsonl"
ETA_LEGACY_SHARED_BREAKER_STATE_PATH = Path.home() / ".jarvis" / "breaker.json"
ETA_LEGACY_DEADMAN_SENTINEL_PATH = Path.home() / ".jarvis" / "operator.sentinel"
ETA_LEGACY_DEADMAN_JOURNAL_PATH = Path.home() / ".jarvis" / "operator_activity.jsonl"
ETA_LEGACY_PROMOTION_STATE_PATH = Path.home() / ".jarvis" / "promotion.json"
ETA_LEGACY_PROMOTION_JOURNAL_PATH = Path.home() / ".jarvis" / "promotion.jsonl"
ETA_LEGACY_AVENGERS_JOURNAL_PATH = Path.home() / ".jarvis" / "avengers.jsonl"
ETA_LEGACY_CALIBRATION_JOURNAL_PATH = Path.home() / ".jarvis" / "calibration.jsonl"


def ensure_dir(path: Path) -> Path:
    """Create a directory tree when a script writes outputs there."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_parent(path: Path) -> Path:
    """Create the parent directory for a file path and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def default_alerts_log_path() -> Path:
    """Prefer the canonical runtime alert log, with legacy docs fallback.

    Read-only diagnostics use this so older snapshots remain inspectable
    while live runtime writes stay out of tracked docs.
    """
    if ETA_RUNTIME_ALERTS_LOG_PATH.exists() or not ETA_LEGACY_DOCS_ALERTS_LOG_PATH.exists():
        return ETA_RUNTIME_ALERTS_LOG_PATH
    return ETA_LEGACY_DOCS_ALERTS_LOG_PATH


def default_runtime_log_path() -> Path:
    """Prefer the canonical runtime log, with legacy docs fallback."""
    if ETA_RUNTIME_LOG_PATH.exists() or not ETA_LEGACY_DOCS_RUNTIME_LOG_PATH.exists():
        return ETA_RUNTIME_LOG_PATH
    return ETA_LEGACY_DOCS_RUNTIME_LOG_PATH


def default_drift_watchdog_log_path() -> Path:
    """Prefer canonical drift-watchdog state, with legacy docs fallback."""
    if ETA_DRIFT_WATCHDOG_LOG_PATH.exists() or not ETA_LEGACY_DOCS_DRIFT_WATCHDOG_LOG_PATH.exists():
        return ETA_DRIFT_WATCHDOG_LOG_PATH
    return ETA_LEGACY_DOCS_DRIFT_WATCHDOG_LOG_PATH


def default_btc_live_decisions_path() -> Path:
    """Prefer canonical BTC live decisions, with legacy docs fallback."""
    if ETA_BTC_LIVE_DECISIONS_PATH.exists() or not ETA_LEGACY_BTC_LIVE_DECISIONS_PATH.exists():
        return ETA_BTC_LIVE_DECISIONS_PATH
    return ETA_LEGACY_BTC_LIVE_DECISIONS_PATH


def default_integrations_live_status_path() -> Path:
    """Prefer canonical integrations live-status, with legacy docs fallback."""
    if ETA_INTEGRATIONS_LIVE_STATUS_PATH.exists() or not ETA_LEGACY_INTEGRATIONS_LIVE_STATUS_PATH.exists():
        return ETA_INTEGRATIONS_LIVE_STATUS_PATH
    return ETA_LEGACY_INTEGRATIONS_LIVE_STATUS_PATH


def default_premarket_inputs_path() -> Path:
    """Prefer canonical premarket inputs, with legacy docs fallback."""
    if ETA_PREMARKET_INPUTS_PATH.exists() or not ETA_LEGACY_PREMARKET_INPUTS_PATH.exists():
        return ETA_PREMARKET_INPUTS_PATH
    return ETA_LEGACY_PREMARKET_INPUTS_PATH


def default_paper_run_report_path() -> Path:
    """Prefer canonical paper-run report, with legacy docs fallback."""
    if ETA_PAPER_RUN_REPORT_PATH.exists() or not ETA_LEGACY_PAPER_RUN_REPORT_PATH.exists():
        return ETA_PAPER_RUN_REPORT_PATH
    return ETA_LEGACY_PAPER_RUN_REPORT_PATH


def default_decisions_v1_path() -> Path:
    """Prefer canonical decisions lock, with legacy docs fallback."""
    if ETA_DECISIONS_V1_PATH.exists() or not ETA_LEGACY_DECISIONS_V1_PATH.exists():
        return ETA_DECISIONS_V1_PATH
    return ETA_LEGACY_DECISIONS_V1_PATH


def default_sharpe_baseline_path() -> Path:
    """Prefer canonical Sharpe baseline, with legacy docs fallback."""
    if ETA_SHARPE_BASELINE_PATH.exists() or not ETA_LEGACY_SHARPE_BASELINE_PATH.exists():
        return ETA_SHARPE_BASELINE_PATH
    return ETA_LEGACY_SHARPE_BASELINE_PATH


def default_weekly_review_latest_path() -> Path:
    """Prefer canonical weekly review latest, with legacy docs fallback."""
    if ETA_WEEKLY_REVIEW_LATEST_JSON_PATH.exists() or not ETA_LEGACY_WEEKLY_REVIEW_LATEST_JSON_PATH.exists():
        return ETA_WEEKLY_REVIEW_LATEST_JSON_PATH
    return ETA_LEGACY_WEEKLY_REVIEW_LATEST_JSON_PATH


def default_kill_log_path() -> Path:
    """Prefer canonical kill-log state, with legacy docs fallback."""
    if ETA_KILL_LOG_PATH.exists() or not ETA_LEGACY_KILL_LOG_PATH.exists():
        return ETA_KILL_LOG_PATH
    return ETA_LEGACY_KILL_LOG_PATH


def default_preflight_dryrun_report_path() -> Path:
    """Prefer canonical preflight dryrun report, with legacy docs fallback."""
    if ETA_PREFLIGHT_DRYRUN_REPORT_PATH.exists() or not ETA_LEGACY_PREFLIGHT_DRYRUN_REPORT_PATH.exists():
        return ETA_PREFLIGHT_DRYRUN_REPORT_PATH
    return ETA_LEGACY_PREFLIGHT_DRYRUN_REPORT_PATH
