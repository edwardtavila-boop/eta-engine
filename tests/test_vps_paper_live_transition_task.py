from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deploy" / "scripts" / "run_paper_live_transition_check.cmd"
REGISTRAR = ROOT / "deploy" / "scripts" / "register_paper_live_transition_check_task.ps1"
RESET_AUDIT_RUNNER = ROOT / "deploy" / "scripts" / "run_daily_stop_reset_audit_task.cmd"
RESET_AUDIT_REGISTRAR = ROOT / "deploy" / "scripts" / "register_daily_stop_reset_audit_task.ps1"
DIAGNOSTICS_CACHE_WARM_RUNNER = ROOT / "deploy" / "scripts" / "run_dashboard_diagnostics_cache_warm.ps1"
DIAGNOSTICS_CACHE_WARM_REGISTRAR = ROOT / "deploy" / "scripts" / "register_dashboard_diagnostics_cache_warm_task.ps1"
READINESS_RUNNER = ROOT / "deploy" / "scripts" / "run_eta_readiness_snapshot.cmd"
READINESS_REGISTRAR = ROOT / "deploy" / "scripts" / "register_eta_readiness_snapshot_task.ps1"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
RUNBOOK = ROOT / "docs" / "live_launch_runbook.md"


def test_paper_live_transition_runner_writes_canonical_cache_without_task_failure() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state" in text
    assert r"ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine" in text
    assert "python.exe" in text
    assert "-m eta_engine.scripts.bot_strategy_readiness" in text
    assert "--scope supervisor_pinned" in text
    assert "--snapshot" in text
    assert "READINESS_STDOUT_TMP=%ETA_LOG_DIR%\\bot_strategy_readiness.%RUN_ID%.stdout.tmp.log" in text
    assert "READINESS_STDERR_TMP=%ETA_LOG_DIR%\\bot_strategy_readiness.%RUN_ID%.stderr.tmp.log" in text
    assert "bot_strategy_readiness.stdout.log" in text
    assert "bot_strategy_readiness.stderr.log" in text
    assert "bot_strategy_readiness.task.log" in text
    assert "-m eta_engine.scripts.paper_live_transition_check" in text
    assert "RUN_ID=%RANDOM%_%RANDOM%" in text
    assert "STDOUT_TMP=%ETA_LOG_DIR%\\paper_live_transition_check.%RUN_ID%.stdout.tmp.log" in text
    assert "STDERR_TMP=%ETA_LOG_DIR%\\paper_live_transition_check.%RUN_ID%.stderr.tmp.log" in text
    assert "paper_live_transition_check.stdout.log" in text
    assert "paper_live_transition_check.stderr.log" in text
    assert "paper_live_transition_check.task.log" in text
    assert "exit_code=%CHECK_RC%" in text
    assert '1> "%STDOUT_TMP%"' in text
    assert '2> "%STDERR_TMP%"' in text
    assert '1>> "%ETA_LOG_DIR%\\paper_live_transition_check.stdout.log"' not in text
    assert "exit /b 0" in text


def test_paper_live_transition_registrar_is_canonical_and_low_overhead() -> None:
    text = REGISTRAR.read_text(encoding="utf-8")

    assert "ETA-PaperLiveTransitionCheck" in text
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in text
    assert "Assert-CanonicalEtaPath" in text
    assert "run_paper_live_transition_check.cmd" in text
    assert "paper_live_transition_check.json" in text
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in text
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in text
    assert "Start-ScheduledTask -TaskName $TaskName" in text


def test_paper_live_transition_task_is_wired_into_bootstrap_and_runbook() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "register_paper_live_transition_check_task.ps1" in bootstrap
    assert "ETA-PaperLiveTransitionCheck" in bootstrap
    assert "paper-live transition cache refresher task" in bootstrap
    assert "register_paper_live_transition_check_task.ps1 -Start" in runbook
    assert "never clears holds or submits orders" in runbook


def test_paper_live_transition_task_scripts_do_not_use_legacy_write_paths() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            RUNNER,
            REGISTRAR,
            RESET_AUDIT_RUNNER,
            RESET_AUDIT_REGISTRAR,
            DIAGNOSTICS_CACHE_WARM_RUNNER,
            DIAGNOSTICS_CACHE_WARM_REGISTRAR,
            READINESS_RUNNER,
            READINESS_REGISTRAR,
        )
    )

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "mnq_data" not in combined
    assert "crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined


def test_daily_stop_reset_audit_runner_refreshes_canonical_cache_without_task_failure() -> None:
    text = RESET_AUDIT_RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_STATE_DIR=%ETA_ROOT%\var\eta_engine\state" in text
    assert r"ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine" in text
    assert "-m eta_engine.scripts.daily_stop_reset_audit" in text
    assert "daily_stop_reset_audit_latest.json" in text
    assert "daily_stop_reset_audit.stdout.log" in text
    assert "daily_stop_reset_audit.stderr.log" in text
    assert "daily_stop_reset_audit.task.log" in text
    assert "exit_code=%AUDIT_RC%" in text
    assert "exit /b 0" in text


def test_daily_stop_reset_audit_registrar_is_wired_into_bootstrap() -> None:
    registrar = RESET_AUDIT_REGISTRAR.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    assert "ETA-DailyStopResetAudit" in registrar
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in registrar
    assert "Assert-CanonicalEtaPath" in registrar
    assert "run_daily_stop_reset_audit_task.cmd" in registrar
    assert "daily_stop_reset_audit_latest.json" in registrar
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in registrar
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)" in registrar
    assert "-MultipleInstances IgnoreNew" in registrar
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in registrar
    assert "Order action: never submits, cancels, flattens, or promotes" in registrar
    assert "register_daily_stop_reset_audit_task.ps1" in bootstrap
    assert "daily stop reset audit task" in bootstrap
    assert "register_daily_stop_reset_audit_task.ps1 -Start" in RUNBOOK.read_text(encoding="utf-8")


def test_dashboard_diagnostics_cache_warmer_is_wired_into_bootstrap() -> None:
    runner = DIAGNOSTICS_CACHE_WARM_RUNNER.read_text(encoding="utf-8")
    registrar = DIAGNOSTICS_CACHE_WARM_REGISTRAR.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "http://127.0.0.1:8421/api/dashboard/diagnostics?refresh=1" in runner
    assert "dashboard_diagnostics_cache_warm.task.log" in runner
    assert "Iterations = 3" in runner
    assert "SleepSeconds = 20" in runner
    assert "exit 0" in runner
    assert "ETA-DashboardDiagnosticsCacheWarm" in registrar
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in registrar
    assert "Assert-CanonicalEtaPath" in registrar
    assert "run_dashboard_diagnostics_cache_warm.ps1" in registrar
    assert "-RepetitionInterval (New-TimeSpan -Minutes 1)" in registrar
    assert "-ExecutionTimeLimit (New-TimeSpan -Seconds 75)" in registrar
    assert "-MultipleInstances IgnoreNew" in registrar
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in registrar
    assert "Order action: never submits, cancels, flattens, or promotes" in registrar
    assert "register_dashboard_diagnostics_cache_warm_task.ps1" in bootstrap
    assert "dashboard diagnostics cache warmer task" in bootstrap
    assert "register_dashboard_diagnostics_cache_warm_task.ps1 -Start" in runbook


def test_eta_readiness_snapshot_runner_refreshes_canonical_ops_receipt_without_order_actions() -> None:
    text = READINESS_RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert r"ETA_OPS_DIR=%ETA_ROOT%\var\ops" in text
    assert r"ETA_LOG_DIR=%ETA_ROOT%\logs\eta_engine" in text
    assert r"%ETA_ROOT%\scripts\eta-readiness-snapshot.ps1" in text
    assert '-StatusPath "%ETA_OPS_DIR%\\eta_readiness_snapshot_latest.json"' in text
    assert "-Json" in text
    assert "eta_readiness_snapshot.stdout.log" in text
    assert "eta_readiness_snapshot.stderr.log" in text
    assert "eta_readiness_snapshot.task.log" in text
    assert "exit_code=%SNAPSHOT_RC%" in text
    assert "never submits" not in text.lower()
    assert "exit /b %SNAPSHOT_RC%" in text


def test_eta_readiness_snapshot_wrapper_keeps_live_readiness_url_on_same_base() -> None:
    wrapper = (ROOT.parent / "scripts" / "eta-readiness-snapshot.ps1").read_text(encoding="utf-8")

    assert '$LiveReadinessUrl = "$BaseUrl/api/jarvis/bot_strategy_readiness/volume_profile_mnq"' in wrapper
    assert '--live-readiness-url", $LiveReadinessUrl' in wrapper
    assert (
        '--live-readiness-url", "$PublicFallbackUrl/api/jarvis/bot_strategy_readiness/volume_profile_mnq"'
        in wrapper
    )
    assert "paper_live_transition_check" in wrapper
    assert "non_authoritative_gateway_host" in wrapper
    assert '$publicFallbackReason = "non_authoritative_gateway_host"' in wrapper


def test_eta_readiness_snapshot_registrar_is_wired_into_bootstrap() -> None:
    registrar = READINESS_REGISTRAR.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    assert "ETA-Readiness-Snapshot" in registrar
    assert '$WorkspaceRoot = "C:\\EvolutionaryTradingAlgo"' in registrar
    assert "Assert-CanonicalEtaPath" in registrar
    assert "run_eta_readiness_snapshot.cmd" in registrar
    assert "eta_readiness_snapshot_latest.json" in registrar
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in registrar
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 3)" in registrar
    assert "-MultipleInstances IgnoreNew" in registrar
    assert '-UserId "NT AUTHORITY\\SYSTEM"' in registrar
    assert "Order action: never submits, cancels, flattens, or promotes" in registrar
    assert "register_eta_readiness_snapshot_task.ps1" in bootstrap
    assert "ETA readiness snapshot refresher task" in bootstrap
