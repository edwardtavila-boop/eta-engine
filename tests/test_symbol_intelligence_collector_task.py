from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRAR = ROOT / "deploy" / "scripts" / "register_symbol_intelligence_collector_task.ps1"
RUNNER = ROOT / "deploy" / "scripts" / "run_symbol_intelligence_collector.cmd"
BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"
REAUTH_REGISTRAR = ROOT / "deploy" / "scripts" / "register_ibgateway_reauth_task.ps1"


def test_symbol_intelligence_collector_runner_is_canonical_and_logged():
    text = RUNNER.read_text(encoding="utf-8")

    assert r"ETA_ROOT=C:\EvolutionaryTradingAlgo" in text
    assert "ETA_STATE_DIR=%ETA_ROOT%\\var\\eta_engine\\state" in text
    assert "ETA_LOG_DIR=%ETA_ROOT%\\logs\\eta_engine" in text
    assert "eta_engine.scripts.symbol_intelligence_collector --json" in text
    assert "symbol_intelligence_collector.stdout.log" in text
    assert "symbol_intelligence_collector.stderr.log" in text
    assert "exit /b %COLLECTOR_RC%" in text
    assert "OneDrive" not in text
    assert "LOCALAPPDATA" not in text
    assert "C:\\mnq_data" not in text


def test_symbol_intelligence_collector_task_registers_24x7_vps_heartbeat():
    text = REGISTRAR.read_text(encoding="utf-8")

    assert 'TaskName = "ETA-SymbolIntelCollector"' in text
    assert r"C:\EvolutionaryTradingAlgo" in text
    assert "run_symbol_intelligence_collector.cmd" in text
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskTrigger -AtLogOn" in text
    assert "RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in text
    assert "IntervalMinutes -lt 1 -or $IntervalMinutes -gt 30" in text
    assert "MultipleInstances IgnoreNew" in text
    assert "RestartCount 3" in text
    assert "NT AUTHORITY\\SYSTEM" in text
    assert "LogonType Interactive" in text
    assert "current_user:$currentUser" in text
    assert "RetireLegacyTier2Task" in text
    assert "EtaTier2SnapshotSync" in text


def test_vps_bootstrap_registers_symbol_intelligence_collector():
    text = BOOTSTRAP.read_text(encoding="utf-8")

    assert "register_symbol_intelligence_collector_task.ps1" in text
    assert "ETA-SymbolIntelCollector" in text
    assert "symbol intelligence collector task" in text.lower()


def test_ibgateway_reauth_task_has_current_user_fallback_for_non_elevated_repair():
    text = REAUTH_REGISTRAR.read_text(encoding="utf-8")

    assert "[switch]$CurrentUser" in text
    assert "New-ScheduledTaskTrigger -AtLogOn -User $currentUserName" in text
    assert "current_user:$currentUserName" in text
    assert "SYSTEM registration was unavailable" in text
