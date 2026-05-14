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
    assert workspace_roots.ETA_IBC_CUTOVER_READINESS_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "ibc_cutover_readiness.json"
    )
    assert workspace_roots.ETA_BOT_STRATEGY_READINESS_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "bot_strategy_readiness_latest.json"
    )
    assert workspace_roots.ETA_PAPER_LIVE_LAUNCH_CHECK_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "paper_live_launch_check_latest.json"
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
    assert workspace_roots.ETA_PROP_OPERATOR_CHECKLIST_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "prop_operator_checklist_latest.json"
    )
    assert workspace_roots.ETA_PROP_STRATEGY_PROMOTION_AUDIT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "prop_strategy_promotion_audit_latest.json"
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
    assert workspace_roots.ETA_LEGACY_JARVIS_TRADE_CLOSES_PATH == (
        ROOT / "eta_engine" / "state" / "jarvis_intel" / "trade_closes.jsonl"
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
    assert "workspace_roots.ETA_RESEARCH_GRID_RUNTIME_DIR" in _read("eta_engine/scripts/run_research_grid.py")
    assert "ETA_LIVE_DATA_RUNTIME_DIR" in _read("eta_engine/scripts/dual_data_collector.py")
    assert 'ROOT / "docs" / "live_data"' not in _read("eta_engine/scripts/dual_data_collector.py")
    assert "ETA_RUNTIME_DECISION_JOURNAL_PATH" in _read("eta_engine/scripts/announce_data_library.py")
    assert "ETA_RUNTIME_DECISION_JOURNAL_PATH" in _read("eta_engine/scripts/drift_check.py")
    assert "ETA_RUNTIME_DECISION_JOURNAL_PATH" in _read("eta_engine/scripts/monte_carlo_stress.py")
    assert "eta_engine\\docs\\decision_journal.jsonl" not in _read("eta_engine/scripts/runtime_readiness_check.ps1")
    assert "ETA_RUNTIME_DECISION_JOURNAL_PATH" in _read("eta_engine/brain/jarvis_v3/health_check.py")
    assert "workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH" in _read("eta_engine/scripts/operator_queue_snapshot.py")
    assert "workspace_roots.ETA_OPERATOR_QUEUE_PREVIOUS_SNAPSHOT_PATH" in _read(
        "eta_engine/scripts/operator_queue_snapshot.py"
    )
    assert "workspace_roots.ETA_IBC_CUTOVER_READINESS_PATH" in _read("eta_engine/scripts/ibc_cutover_readiness.py")
    assert "workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH" in _read("eta_engine/scripts/operator_queue_heartbeat.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read("eta_engine/obs/heartbeat_writer.py")
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read("eta_engine/obs/daemon_recovery_watchdog.py")
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
    # (in that order — multi-line call).
    assert "provider,\n        regime_provider,\n        args.etf_path" in text


def test_b_class_state_writers_use_canonical_var_state_path() -> None:
    """B-class state-file writers (LEGACY_PATH_AUDIT.md) write canonical.

    After the 2026-05-04 migration, the B1–B5 writers must default to
    ``var/eta_engine/state`` and only consult the legacy in-repo
    ``eta_engine/state`` path as a read fallback. The string checks
    below pin both halves of that contract.
    """
    # B1: dashboard_api.py default state dir is now canonical, with the
    # legacy in-repo path kept only as a labelled fallback.
    dashboard_api = _read("eta_engine/deploy/scripts/dashboard_api.py")
    assert '_DEFAULT_STATE = _WORKSPACE_ROOT / "var" / "eta_engine" / "state"' in dashboard_api
    # ruff/black collapsed the column-alignment to single space — keep the
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
    # The triple fan-out is gone — the `latch_paths = [...]` literal
    # that listed three destinations should no longer appear.
    assert "latch_paths = [" not in hermes

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


def test_b_class_kill_switch_latch_default_resolves_to_canonical_workspace() -> None:
    """run_eta_live default latch path lands under workspace var/."""
    scripts_text = _read("eta_engine/scripts/run_eta_live.py")
    feeds_text = _read("eta_engine/feeds/run_eta_live.py")
    for text in (scripts_text, feeds_text):
        # New canonical default: WORKSPACE_ROOT/var/eta_engine/state/kill_switch_latch.json
        assert ('WORKSPACE_ROOT / "var" / "eta_engine" / "state" / "kill_switch_latch.json"') in text
        # The legacy default path (ROOT / "state" / "kill_switch_latch.json")
        # must no longer appear as a write target.
        assert 'ROOT / "state" / "kill_switch_latch.json"' not in text
        # The runtime should consult the legacy path only via the
        # read-fallback helper, not as a direct constant.
        assert "default_legacy_path()" in text


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
    # The legacy in-repo path is gone from the installer's write target.
    assert "'eta_engine\\state\\fm_health.json'" not in installer_text
