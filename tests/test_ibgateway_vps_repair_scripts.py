from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "deploy" / "scripts" / "start_ibgateway.ps1"
REPAIR = ROOT / "deploy" / "scripts" / "repair_ibgateway_vps.ps1"
INSTALL = ROOT / "deploy" / "scripts" / "install_ibgateway_1046.ps1"
IBC_INSTALL = ROOT / "deploy" / "scripts" / "install_ibc.ps1"
IBC_CREDENTIALS = ROOT / "deploy" / "scripts" / "set_ibc_credentials.ps1"
GATEWAY_AUTHORITY = ROOT / "deploy" / "scripts" / "set_gateway_authority.ps1"
DISABLE_LOCAL_GATEWAY = ROOT / "deploy" / "scripts" / "disable_non_authoritative_gateway_tasks.ps1"
VPS_BOOTSTRAP = ROOT / "deploy" / "vps_bootstrap.ps1"


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
    assert "function Wait-NoApiListener" in text
    assert "gateway API listener ready" in text
    assert "StartupTimeoutSeconds" in text
    assert "[int]$StartupTimeoutSeconds = 600" in text
    assert 'Get-Process -Name "ibgateway", "ibgateway1" -ErrorAction SilentlyContinue' in text
    assert "function Get-IbcManagedGatewayProcesses" in text
    assert "ibcalpha.ibc.ibcgateway" in text
    assert "scripts\\startibc.bat" in text
    assert "function Get-ProcessIdValue" in text
    assert "Stop-Process -Id $procId -Force" in text
    assert "stopping existing gateway API listener owner" in text
    assert "Timed out waiting for IB Gateway API listener to exit" in text
    assert 'CommandLine -like "*ibgateway*"' not in text
    assert "$existingGateway = @(Get-GatewayProcesses)" in text
    assert "gateway process running without API listener" in text
    assert "existing gateway process running; no start needed" in text


def test_ibgateway_starter_requires_explicit_vps_authority() -> None:
    text = STARTER.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\gateway_authority.json" in text
    assert "[switch]$AllowNonVpsGatewayStart" in text
    assert "function Assert-GatewayAuthority" in text
    assert "ETA policy: the VPS is the 24/7 Gateway deployment source" in text
    assert "deploy\\scripts\\set_gateway_authority.ps1 -Apply -Role vps" in text
    assert "Assert-GatewayAuthority -Path $GatewayAuthorityPath" in text
    assert "ETA_IBKR_GATEWAY_AUTHORITY" in text


def test_ibgateway_scripts_treat_ibc_renamed_executable_as_canonical_runtime() -> None:
    starter_text = STARTER.read_text(encoding="utf-8")
    repair_text = REPAIR.read_text(encoding="utf-8")

    for text in (starter_text, repair_text):
        assert 'Join-Path $GatewayInstallDir "ibgateway1.exe"' in text
        assert "function Test-GatewayInstallDir" in text
        assert "function Resolve-GatewayExecutablePath" in text
        assert "ibgateway.exe or ibgateway1.exe" in text

    assert "direct launcher is using ibgateway1.exe because IBC has renamed the canonical executable" in starter_text
    assert "executable_name = if ($exe) { Split-Path -Leaf $exe } else { $null }" in repair_text


def test_ibgateway_starter_does_not_force_restart_healthy_authenticated_gateway() -> None:
    text = STARTER.read_text(encoding="utf-8")

    assert "[switch]$AllowHealthyRestart" in text
    assert "ForceRestart requested but healthy API listener is already present" in text
    assert "skipping restart to preserve authenticated IBKR session" in text
    assert "if ($listener -and $ForceRestart -and -not $AllowHealthyRestart)" in text


def test_ibgateway_starter_supports_ibc_managed_launch() -> None:
    text = STARTER.read_text(encoding="utf-8")

    assert "[switch]$UseIbc" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\tools\ibc" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibc_install.json" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\ibc\private\config.ini" in text
    assert r"C:\EvolutionaryTradingAlgo\eta_engine\secrets\ibkr_credentials.json" in text
    assert "Resolve-IbcInstallDir" in text
    assert "Resolve-IbcCredentials" in text
    assert "[string]$DefaultUserId" in text
    assert "$DefaultUserId" in text
    assert "-DefaultUserId $LoginProfile" in text
    assert "Write-IbcConfig" in text
    assert "StartIBC.bat" in text
    assert '"/Gateway"' in text
    assert '"/IbcPath:$ibcInstallDir"' in text
    assert '"/Config:$IbcConfigPath"' in text
    assert '"/TwsPath:$twsRootDir"' in text
    assert '"/TwsSettingsPath:$GatewayDir"' in text
    assert '"/Mode:$($IbcTradingMode.ToLowerInvariant())"' in text
    assert '"/On2FATimeout:$($IbcTwoFactorTimeoutAction.ToLowerInvariant())"' in text
    assert '[ValidateSet("manual", "primary", "primaryoverride", "secondary")]' in text
    assert '[string]$IbcExistingSessionDetectedAction = "primary"' in text
    assert '[string]$IbcAcceptIncomingConnectionAction = "accept"' in text
    assert '"ExistingSessionDetectedAction=$ExistingSessionDetectedAction"' in text
    assert '"AcceptIncomingConnectionAction=$AcceptIncomingConnectionAction"' in text
    assert "OverrideTwsApiPort=$ApiPort" in text
    assert "IBC launch requires IBKR credentials" in text
    assert "-IbcUserId / -LoginProfile / -IbcPassword / -IbcPasswordFile" in text
    assert "Test-IbcSecretSentinel" in text
    assert "REAL_IBKR_PASSWORD" in text
    assert "Get-UsableIbcSecret" in text


def test_ibgateway_repair_profile_is_low_memory_and_backed_up() -> None:
    text = REPAIR.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\backups\ibgateway" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibgateway_repair.json" in text
    assert '[string]$JtsGatewayRoot = "C:\\Jts\\ibgateway"' in text
    assert '[string]$CanonicalGatewayVersion = "1046"' in text
    assert '[string]$TaskUser = ""' in text
    assert '[string]$TaskPassword = ""' in text
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
    assert '/RU `"$escapedTaskUser`" /RP `"$escapedTaskPassword`"' in text
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
    assert "Enable-TaskIfPresent" in text
    assert "clientportal.gw" in text
    assert "gateway_task_canonical" in text
    assert "task_states" in text
    assert "Get-PortListenerSnapshot" in text


def test_ibgateway_repair_can_switch_tasks_to_ibc_launcher() -> None:
    text = REPAIR.read_text(encoding="utf-8")

    assert "[switch]$UseIbc" in text
    assert 'launcher_mode = if ($UseIbc) { "ibc" } else { "direct" }' in text
    assert "Resolve-TaskUserForTask" in text
    assert "-TaskRunAsUser (Resolve-TaskUserForTask -TaskName $TaskName)" in text
    assert "-TaskRunAsPassword $TaskPassword" in text
    assert "if ($UseIbc) {" in text
    assert '$baseArgs += " -UseIbc"' in text
    assert '$baseArgs += " -IbcPasswordFile `"$IbcPasswordFile`""' in text
    assert '$result.single_source.legacy_tasks."ETA-IBGateway" = Enable-TaskIfPresent -TaskName "ETA-IBGateway"' in text
    assert '$etaGatewayState -ne "Disabled"' in text
    assert (
        "& $Starter -GatewayDir $GatewayDir -LoginProfile $LoginProfile "
        "-ApiPort $ApiPort -UseIbc:$UseIbc -IbcPasswordFile $IbcPasswordFile -ForceRestart" in text
    )


def test_ibgateway_repair_scripts_do_not_reintroduce_legacy_workspace_paths() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (STARTER, REPAIR, INSTALL, IBC_INSTALL, IBC_CREDENTIALS)
    )

    assert "OneDrive" not in combined
    assert "LOCALAPPDATA" not in combined
    assert "mnq_data" not in combined
    assert "crypto_data" not in combined
    assert "TheFirm" not in combined
    assert "The_Firm" not in combined


def test_gateway_authority_helpers_block_workstation_ownership_and_clean_local_tasks() -> None:
    authority_text = GATEWAY_AUTHORITY.read_text(encoding="utf-8")
    disable_text = DISABLE_LOCAL_GATEWAY.read_text(encoding="utf-8")
    bootstrap_text = VPS_BOOTSTRAP.read_text(encoding="utf-8")

    assert "[switch]$AllowDesktopHost" in authority_text
    assert "ProductType -eq 1" in authority_text
    assert "Refusing to mark workstation host" in authority_text
    assert "gateway_authority.json" in authority_text
    assert "$gatewayAuthorityScript = \"$EtaEngineDir\\deploy\\scripts\\set_gateway_authority.ps1\"" in bootstrap_text
    assert "-File $gatewayAuthorityScript -Apply -Role vps" in bootstrap_text
    assert "$gatewayAuthorityReady = $false" in bootstrap_text
    assert 'if ($LASTEXITCODE -eq 0)' in bootstrap_text
    assert "Skipping IBKR Gateway task registration" in bootstrap_text
    assert "if ($gatewayAuthorityReady)" in bootstrap_text
    assert bootstrap_text.index("set_gateway_authority.ps1") < bootstrap_text.index("register_tws_watchdog_task.ps1")

    assert "disable_non_authoritative_gateway_tasks.ps1" in disable_text
    assert "ETA-IBGateway-RunNow" in disable_text
    assert "ETA-IBGateway-Reauth" in disable_text
    assert "ETA-TWS-Watchdog" in disable_text
    assert "EtaIbkrBbo1mCapture" in disable_text
    assert "Non-authoritative Windows workstation" in disable_text


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
    assert 'if ($signature.Status -ne "Valid" -and -not $AllowUnsignedInstaller)' in text
    assert "IB Gateway 10.46 is not installed at C:\\Jts\\ibgateway\\1046" in text
    assert "Rerun with -Install -RepairAfterInstall" in text
    assert "-AllowUnsignedInstaller after confirming the official IBKR download source" in text
    assert "-DexecuteLauncherAction=false" in text
    assert "repair_ibgateway_vps.ps1" in text
    assert "$RepairAfterInstall" in text


def test_ibc_installer_is_canonical_and_uses_official_github_release() -> None:
    text = IBC_INSTALL.read_text(encoding="utf-8")

    assert "https://api.github.com/repos/IbcAlpha/IBC/releases/latest" in text
    assert "https://api.github.com/repos/IbcAlpha/IBC/releases/tags/" in text
    assert "IBCWin-*.zip" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\downloads\ibc" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\tools\ibc" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibc_install.json" in text
    assert "Resolve-ReleaseAsset" in text
    assert "Find-IbcPayloadDir" in text
    assert "Expand-Archive" in text
    assert "scripts\\StartIBC.bat" in text
    assert "EvolutionaryTradingAlgo-IBCInstaller" in text
    assert "download_sha256" in text
    assert "current_install_dir" in text


def test_ibc_credentials_helper_seeds_machine_env_without_logging_values() -> None:
    text = IBC_CREDENTIALS.read_text(encoding="utf-8")

    assert r"C:\EvolutionaryTradingAlgo\eta_engine\secrets\ibkr_credentials.json" in text
    assert r"C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibkr_pw.txt" in text
    assert "PromptForPassword" in text
    assert "PasswordFilePath" in text
    assert "SkipPasswordFile" in text
    assert "Resolve-JsonLoginId" in text
    assert "Write-PasswordFile" in text
    assert "Protect-SecretFilePath" in text
    assert "Set-SecretEnvironmentVariable" in text
    assert "WindowsIdentity]::GetCurrent().Name" in text
    assert "FileSystemRights]::FullControl" in text
    assert "Set-Acl -LiteralPath $Path -AclObject $acl" in text
    assert 'Start-Process -FilePath "icacls.exe"' in text
    assert '"/grant:r"' in text
    assert "IBC_PASSWORD_FILE_WRITABLE=warning" in text
    assert "IBC_PASSWORD_FILE_READONLY=warning" in text
    assert "Set-Content -LiteralPath $Path -Value $Secret -Encoding UTF8 -NoNewline" in text
    assert 'Set-SecretEnvironmentVariable -Name "ETA_IBC_LOGIN_ID"' in text
    assert 'Set-SecretEnvironmentVariable -Name "ETA_IBC_PASSWORD"' in text
    assert 'SetEnvironmentVariable($Name, $Value, "Machine")' in text
    assert 'SetEnvironmentVariable($Name, $Value, "User")' in text
    assert "ETA_IBC_LOGIN_ID=seeded" in text
    assert "ETA_IBC_PASSWORD=seeded" in text
    assert "ETA_IBC_LOGIN_ID_SCOPE=" in text
    assert "ETA_IBC_PASSWORD_SCOPE=" in text
    assert "IBC_PASSWORD_FILE=seeded" in text
    assert "Missing IBKR password" in text
    assert "Write-Output $resolvedPassword" not in text
