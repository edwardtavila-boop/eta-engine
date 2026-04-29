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
    assert workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "operator_queue_snapshot.json"
    )
    assert workspace_roots.ETA_DRIFT_WATCHDOG_LOG_PATH == (
        ROOT / "var" / "eta_engine" / "state" / "drift_watchdog.jsonl"
    )
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
        "eta_engine/scripts/drift_watchdog_smoke.py",
        "eta_engine/scripts/operator_queue_snapshot.py",
        "eta_engine/scripts/runtime_log_smoke.py",
        "eta_engine/scripts/vps_failover_summary.py",
        "eta_engine/deploy/scripts/live_claude_smoke.py",
        "eta_engine/deploy/scripts/register_cloudflare_quick.ps1",
        "eta_engine/deploy/scripts/run_dashboard_8421.ps1",
        "eta_engine/deploy/uninstall_windows.ps1",
        "eta_engine/obs/daemon_recovery_watchdog.py",
        "eta_engine/obs/heartbeat_writer.py",
    )
    for rel_path in targets:
        text = _read(rel_path)
        assert "LOCALAPPDATA" not in text
        assert r"AppData\Local\eta_engine" not in text

    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read(
        "eta_engine/deploy/scripts/live_claude_smoke.py"
    )
    assert "ETA_DRIFT_WATCHDOG_LOG_PATH" in _read("eta_engine/scripts/drift_watchdog_smoke.py")
    assert "ETA_RUNTIME_LOG_PATH" in _read("eta_engine/scripts/runtime_log_smoke.py")
    assert "vps_failover_drill.collect_checks" in _read("eta_engine/scripts/vps_failover_summary.py")
    assert "workspace_roots.ETA_OPERATOR_QUEUE_SNAPSHOT_PATH" in _read(
        "eta_engine/scripts/operator_queue_snapshot.py"
    )
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read(
        "eta_engine/obs/heartbeat_writer.py"
    )
    assert "workspace_roots.ETA_RUNTIME_STATE_DIR" in _read(
        "eta_engine/obs/daemon_recovery_watchdog.py"
    )
    assert '$env:ETA_STATE_DIR = $stateDir' in _read(
        "eta_engine/deploy/scripts/run_dashboard_8421.ps1"
    )


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
    assert "provider,\n        args.etf_path" not in text
    assert "provider, regime_provider, args.etf_path" in text
