from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "deploy" / "scripts" / "start_ibgateway.ps1"
REPAIR = ROOT / "deploy" / "scripts" / "repair_ibgateway_vps.ps1"
INSTALL = ROOT / "deploy" / "scripts" / "install_ibgateway_1046.ps1"


def test_ibgateway_starter_uses_canonical_logs_and_verified_direct_start() -> None:
    text = STARTER.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\logs\ibgateway" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state" in text
    assert "ibgateway_start.lock" in text
    assert "Start-Process" in text
    assert "Start-Process -FilePath $exe" in text
    assert 'Start-Process -FilePath "cmd.exe"' not in text
    assert '/c start ""IBGateway""' not in text
    assert "-WindowStyle Hidden" not in text
    assert "ibgateway.exe" in text
    assert "-login=" in text
    assert "function Wait-ApiListener" in text
    assert "gateway API listener ready" in text
    assert "StartupTimeoutSeconds" in text
    assert "[int]$StartupTimeoutSeconds = 600" in text
    assert 'Get-Process -Name "ibgateway" -ErrorAction SilentlyContinue' in text
    assert "function Get-ProcessIdValue" in text
    assert "Stop-Process -Id $procId -Force" in text
    assert 'CommandLine -like "*ibgateway*"' not in text
    assert "$existingGateway = @(Get-GatewayProcesses)" in text
    assert "gateway process running without API listener" in text
    assert "existing gateway process running; no start needed" in text


def test_ibgateway_starter_does_not_force_restart_healthy_authenticated_gateway() -> None:
    text = STARTER.read_text(encoding="utf-8")

    assert "[switch]$AllowHealthyRestart" in text
    assert "ForceRestart requested but healthy API listener is already present" in text
    assert "skipping restart to preserve authenticated IBKR session" in text
    assert "if ($listener -and $ForceRestart -and -not $AllowHealthyRestart)" in text


def test_ibgateway_repair_profile_is_low_memory_and_backed_up() -> None:
    text = REPAIR.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\backups\ibgateway" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibgateway_repair.json" in text
    assert '[string]$JtsGatewayRoot = "C:\\Jts\\ibgateway"' in text
    assert '[string]$CanonicalGatewayVersion = "1046"' in text
    assert '[string]$Heap = "512m"' in text
    assert "[int]$ParallelGCThreads = 2" in text
    assert "[int]$ConcGCThreads = 1" in text
    assert "ETA-IBGateway" in text
    assert "ETA-IBGateway-RunNow" in text
    assert "ETA-IBGateway-DailyRestart" in text
    assert "schtasks.exe" in text
    assert "updated_via_schtasks" in text
    assert "WaitForExit(15000)" in text
    assert "schtasks timed out" in text
    assert "failed:" in text
    assert "restart_error" in text
    assert "Get-JtsIniSnapshot" in text
    assert "Get-VmOptionsSnapshot" in text
    assert "api_port_configured" in text
    assert "trusted_localhost" in text
    assert "api_only_enabled" in text
    assert "low_memory_profile_configured" in text
    assert "gateway_config" in text


def test_ibgateway_repair_enforces_single_1046_source() -> None:
    text = REPAIR.read_text(encoding="utf-8")

    assert "Assert-CanonicalGatewayDir" in text
    assert "Get-GatewayInstallInventory" in text
    assert "non_canonical_installs" in text
    assert "EnforceSingleSource" in text
    assert "StopLegacyIbkrProcesses" in text
    assert "ApexIbkrGatewayReauth" in text
    assert "IBGatewayInstallAtLogon" in text
    assert "ApexIbkrGatewayWatchdog" in text
    assert "Disable-TaskIfPresent" in text
    assert '"ETA-IBGateway" = Disable-TaskIfPresent' in text
    assert "clientportal.gw" in text
    assert "gateway_task_canonical" in text
    assert "task_states" in text
    assert "Get-PortListenerSnapshot" in text


def test_ibgateway_repair_scripts_do_not_reintroduce_legacy_workspace_paths() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (STARTER, REPAIR, INSTALL)
    )

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "mnq_data" not in combined
    assert "crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined


def test_ibgateway_installer_helper_is_canonical_audited_and_guarded() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "download2.interactivebrokers.com/installers/ibgateway/latest-standalone" in text
    assert "ibgateway-latest-standalone-windows-x64.exe" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\downloads\ibgateway" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibgateway_install.json" in text
    assert r"C:\Jts\ibgateway\1046" in text
    assert "Assert-CanonicalEtaPath" in text
    assert "Assert-Gateway1046" in text
    assert "Get-FileHash" in text
    assert "installer_sha256" in text
    assert "Get-AuthenticodeSignature" in text
    assert "authenticode_status" in text
    assert "[switch]$Install" in text
    assert "[switch]$AllowUnsignedInstaller" in text
    assert "if ($signature.Status -ne \"Valid\" -and -not $AllowUnsignedInstaller)" in text
    assert "IB Gateway 10.46 is not installed at C:\\Jts\\ibgateway\\1046" in text
    assert "-Install -AllowUnsignedInstaller -RepairAfterInstall" in text
    assert "-DexecuteLauncherAction=false" in text
    assert "repair_ibgateway_vps.ps1" in text
    assert "$RepairAfterInstall" in text
