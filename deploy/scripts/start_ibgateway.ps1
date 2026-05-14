[CmdletBinding()]
param(
    [string]$GatewayDir = "C:\Jts\ibgateway\1046",
    [string]$LoginProfile = "apexpredatoribkr",
    [string]$LogDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\logs\ibgateway",
    [string]$StateDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\state",
    [string]$HealthStatusPath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\tws_watchdog.json",
    [switch]$UseIbc,
    [string]$IbcInstallRoot = "C:\EvolutionaryTradingAlgo\var\eta_engine\tools\ibc",
    [string]$IbcInstallStatePath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\ibc_install.json",
    [string]$IbcConfigPath = "C:\EvolutionaryTradingAlgo\var\eta_engine\ibc\private\config.ini",
    [string]$IbcCredentialJsonPath = "C:\EvolutionaryTradingAlgo\eta_engine\secrets\ibkr_credentials.json",
    [string]$IbcPasswordFile = "",
    [string]$IbcUserId = "",
    [string]$IbcPassword = "",
    [string]$IbcTradingMode = "paper",
    [string]$IbcTwoFactorTimeoutAction = "exit",
    [string]$IbcAutoRestartTime = "",
    [ValidateSet("manual", "primary", "primaryoverride", "secondary")]
    [string]$IbcExistingSessionDetectedAction = "primary",
    [string]$IbcAcceptIncomingConnectionAction = "accept",
    [int]$IbcLoginDialogDisplayTimeoutSeconds = 180,
    [switch]$IbcMinimizeMainWindow,
    [int]$ApiPort = 4002,
    [int]$StartupTimeoutSeconds = 600,
    [switch]$ForceRestart,
    [switch]$AllowHealthyRestart
)

$ErrorActionPreference = "Stop"

function Normalize-PathString {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\").ToLowerInvariant()
}

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

function Write-LogLine {
    param([string]$Message)
    $stamp = (Get-Date).ToUniversalTime().ToString("o")
    Add-Content -LiteralPath (Join-Path $LogDir "start_ibgateway.log") -Value "$stamp $Message"
}

function Get-GatewayExecutableCandidates {
    param([string]$GatewayInstallDir)

    return @(
        Join-Path $GatewayInstallDir "ibgateway.exe"
        Join-Path $GatewayInstallDir "ibgateway1.exe"
    )
}

function Test-GatewayInstallDir {
    param([string]$GatewayInstallDir)

    foreach ($candidate in (Get-GatewayExecutableCandidates -GatewayInstallDir $GatewayInstallDir)) {
        if (Test-Path -LiteralPath $candidate) {
            return $true
        }
    }

    return $false
}

function Resolve-GatewayExecutablePath {
    param([string]$GatewayInstallDir)

    foreach ($candidate in (Get-GatewayExecutableCandidates -GatewayInstallDir $GatewayInstallDir)) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw "No runnable IB Gateway executable was found under $GatewayInstallDir"
}

function Resolve-GatewayDir {
    param([string]$RequestedPath)

    $candidates = @()
    $resolvedRequest = ""

    if ($RequestedPath) {
        $resolvedRequest = [System.IO.Path]::GetFullPath($RequestedPath)
        if (Test-GatewayInstallDir -GatewayInstallDir $resolvedRequest) {
            return $resolvedRequest
        }
    }

    $envGatewayDir = $env:ETA_IBGATEWAY_DIR
    if ($envGatewayDir) {
        $resolvedEnv = [System.IO.Path]::GetFullPath($envGatewayDir)
        if (Test-GatewayInstallDir -GatewayInstallDir $resolvedEnv) {
            return $resolvedEnv
        }
    }

    $gatewayRoot = if ($resolvedRequest) {
        Split-Path -Parent $resolvedRequest
    } else {
        "C:\Jts\ibgateway"
    }

    if (-not (Test-Path -LiteralPath $gatewayRoot)) {
        throw "IB Gateway root not found: $gatewayRoot"
    }

    $candidates = @(
        Get-ChildItem -Path $gatewayRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-GatewayInstallDir -GatewayInstallDir $_.FullName } |
            Sort-Object `
                @{ Expression = {
                    $digits = [regex]::Match($_.Name, '\d+').Value
                    if ($digits) { [int]$digits } else { 0 }
                }; Descending = $true }, `
                @{ Expression = { $_.LastWriteTimeUtc }; Descending = $true }
    )

    if ($candidates.Count -eq 0) {
        throw "No installed IB Gateway directories with ibgateway.exe or ibgateway1.exe were found under $gatewayRoot"
    }

    return $candidates[0].FullName
}

function Read-JsonObject {
    param([string]$Path)

    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            return $null
        }
        return Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }
}

function Read-DotEnvMap {
    param([string]$Path)

    $result = @{}
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return $result
    }

    foreach ($raw in Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue) {
        $line = [string]$raw
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -lt 2) {
            continue
        }
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim("'").Trim('"')
        if (-not [string]::IsNullOrWhiteSpace($key) -and -not $result.ContainsKey($key)) {
            $result[$key] = $value
        }
    }

    return $result
}

function Read-FirstLineValue {
    param([string]$Path)

    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return ""
    }

    try {
        $line = Get-Content -LiteralPath $Path -TotalCount 1 -ErrorAction Stop
        return [string]$line
    } catch {
        return ""
    }
}

function Test-IbcSecretSentinel {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $true
    }

    $text = $Value.Trim()
    return (
        $text -match "^(REPLACE|PLACEHOLDER|TODO|CHANGEME)" -or
        $text -match "REAL_IBKR_PASSWORD" -or
        $text -match "^<.*password.*>$"
    )
}

function Get-UsableIbcSecret {
    param([object]$Value)

    if ($null -eq $Value) {
        return ""
    }

    $text = ([string]$Value).Trim()
    if (Test-IbcSecretSentinel -Value $text) {
        return ""
    }

    return $text
}

function Get-FirstNonEmptyValue {
    param([object[]]$Candidates)

    foreach ($candidate in $Candidates) {
        if ($null -eq $candidate) {
            continue
        }
        $text = Get-UsableIbcSecret -Value $candidate
        if (-not [string]::IsNullOrWhiteSpace($text)) {
            return $text
        }
    }

    return ""
}

function Resolve-IbcInstallDir {
    param(
        [string]$InstallRoot,
        [string]$StatePath
    )

    Assert-CanonicalEtaPath -Path $InstallRoot
    Assert-CanonicalEtaPath -Path $StatePath

    $envDir = $env:ETA_IBC_DIR
    if ($envDir) {
        $resolvedEnv = [System.IO.Path]::GetFullPath($envDir)
        if (Test-Path -LiteralPath (Join-Path $resolvedEnv "scripts\StartIBC.bat")) {
            return $resolvedEnv
        }
    }

    $state = Read-JsonObject -Path $StatePath
    if ($null -ne $state) {
        foreach ($propertyName in @("current_install_dir", "install_dir")) {
            $candidate = $state.$propertyName
            if ($candidate) {
                $resolvedCandidate = [System.IO.Path]::GetFullPath([string]$candidate)
                if (Test-Path -LiteralPath (Join-Path $resolvedCandidate "scripts\StartIBC.bat")) {
                    return $resolvedCandidate
                }
            }
        }
    }

    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        throw "IBC install root not found: $InstallRoot"
    }

    $candidates = @(
        Get-ChildItem -Path $InstallRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "scripts\StartIBC.bat") } |
            Sort-Object `
                @{ Expression = {
                    $normalized = ($_.Name -replace '[^0-9\.]', '')
                    try {
                        if ([string]::IsNullOrWhiteSpace($normalized)) {
                            return [version]"0.0"
                        }
                        return [version]$normalized
                    } catch {
                        return [version]"0.0"
                    }
                }; Descending = $true }, `
                @{ Expression = { $_.LastWriteTimeUtc }; Descending = $true }
    )

    if ($candidates.Count -eq 0) {
        throw "No installed IBC runtime with scripts\StartIBC.bat was found under $InstallRoot"
    }

    return $candidates[0].FullName
}

function Resolve-IbkrJsonCredentialMap {
    param([string]$Path)

    $result = @{}
    $jsonObject = Read-JsonObject -Path $Path
    if ($null -eq $jsonObject) {
        return $result
    }

    foreach ($property in $jsonObject.PSObject.Properties) {
        $name = [string]$property.Name
        $value = $property.Value
        if (-not [string]::IsNullOrWhiteSpace($name) -and $null -ne $value) {
            $result[$name] = [string]$value
        }
    }

    return $result
}

function Resolve-IbcCredentials {
    param(
        [string]$ExplicitUserId,
        [string]$ExplicitPassword,
        [string]$PasswordFile,
        [string]$CredentialJsonPath,
        [string]$DefaultUserId
    )

    $dotEnv = Read-DotEnvMap -Path "C:\EvolutionaryTradingAlgo\eta_engine\.env"
    $jsonCreds = Resolve-IbkrJsonCredentialMap -Path $CredentialJsonPath
    $passwordFromFile = Read-FirstLineValue -Path $PasswordFile

    $userId = Get-FirstNonEmptyValue -Candidates @(
        $ExplicitUserId,
        $env:ETA_IBC_LOGIN_ID,
        $env:IBKR_USERNAME,
        $env:IBKR_LOGIN_ID,
        $dotEnv["ETA_IBC_LOGIN_ID"],
        $dotEnv["IBKR_USERNAME"],
        $dotEnv["IBKR_LOGIN_ID"],
        $jsonCreds["username"],
        $jsonCreds["user"],
        $jsonCreds["login"],
        $jsonCreds["ib_login_id"],
        $jsonCreds["user_id"],
        $DefaultUserId
    )

    $password = Get-FirstNonEmptyValue -Candidates @(
        $ExplicitPassword,
        $passwordFromFile,
        $env:ETA_IBC_PASSWORD,
        $env:IBKR_PASSWORD,
        $dotEnv["ETA_IBC_PASSWORD"],
        $dotEnv["IBKR_PASSWORD"],
        $jsonCreds["password"],
        $jsonCreds["pass"],
        $jsonCreds["ib_password"]
    )

    return [ordered]@{
        user_id = $userId
        password = $password
    }
}

function Protect-IbcConfigPath {
    param([string]$Path)

    try {
        $aclArgs = @(
            "`"$Path`"",
            "/inheritance:r",
            "/grant:r",
            "$env:USERNAME`:F",
            "/grant:r",
            "SYSTEM`:F"
        )
        Start-Process -FilePath "icacls.exe" -ArgumentList $aclArgs -Wait -NoNewWindow | Out-Null
    } catch {
        Write-LogLine "WARNING unable to harden ACL on IBC config path: $($_.Exception.Message)"
    }
}

function Write-IbcConfig {
    param(
        [string]$Path,
        [string]$UserId,
        [string]$Password,
        [string]$TradingMode,
        [string]$TwoFactorTimeoutAction,
        [string]$AutoRestartTime,
        [string]$ExistingSessionDetectedAction,
        [string]$AcceptIncomingConnectionAction,
        [int]$LoginDialogDisplayTimeoutSeconds,
        [switch]$MinimizeMainWindow
    )

    Assert-CanonicalEtaPath -Path $Path
    $configParent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $configParent | Out-Null

    $mode = if ([string]::IsNullOrWhiteSpace($TradingMode)) { "paper" } else { $TradingMode.ToLowerInvariant() }
    $restartOnTwoFactorTimeout = if ($TwoFactorTimeoutAction.ToLowerInvariant() -eq "restart") { "yes" } else { "no" }
    $minimizeSetting = if ($MinimizeMainWindow) { "yes" } else { "no" }
    $autoRestartSetting = if ([string]::IsNullOrWhiteSpace($AutoRestartTime)) {
        "AutoRestartTime="
    } else {
        "AutoRestartTime=$($AutoRestartTime.Trim())"
    }

    $lines = @(
        "# Managed by start_ibgateway.ps1 for ETA IBC launch.",
        "IbLoginId=$UserId",
        "IbPassword=$Password",
        "TradingMode=$mode",
        "ReloginAfterSecondFactorAuthenticationTimeout=$restartOnTwoFactorTimeout",
        "LoginDialogDisplayTimeout=$LoginDialogDisplayTimeoutSeconds",
        "IbDir=",
        "StoreSettingsOnServer=",
        "MinimizeMainWindow=$minimizeSetting",
        "ExistingSessionDetectedAction=$ExistingSessionDetectedAction",
        "OverrideTwsApiPort=$ApiPort",
        $autoRestartSetting,
        "AcceptIncomingConnectionAction=$AcceptIncomingConnectionAction",
        "CommandServerPort=0"
    )

    Set-Content -LiteralPath $Path -Value $lines -Encoding ASCII
    Protect-IbcConfigPath -Path $Path
}

function Resolve-TwsRootDir {
    param([string]$ResolvedGatewayDir)

    $gatewayParent = Split-Path -Parent $ResolvedGatewayDir
    if ((Split-Path -Leaf $gatewayParent).Equals("ibgateway", [System.StringComparison]::OrdinalIgnoreCase)) {
        return Split-Path -Parent $gatewayParent
    }

    return Split-Path -Parent $ResolvedGatewayDir
}

function Get-IbcManagedGatewayProcesses {
    param(
        [string]$GatewayInstallDir,
        [string]$ConfigPath,
        [string]$InstallRoot
    )

    $resolvedGateway = Normalize-PathString -Path $GatewayInstallDir
    $resolvedConfig = if ($ConfigPath) {
        [System.IO.Path]::GetFullPath($ConfigPath).TrimEnd("\").ToLowerInvariant()
    } else {
        ""
    }
    $resolvedInstallRoot = if ($InstallRoot) {
        [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd("\").ToLowerInvariant()
    } else {
        ""
    }

    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $cmd = [string]$_.CommandLine
            if ([string]::IsNullOrWhiteSpace($cmd)) {
                $false
            } else {
                $lower = $cmd.ToLowerInvariant()
                $isIbcGateway = $lower.Contains("ibcalpha.ibc.ibcgateway") -or $lower.Contains("scripts\startibc.bat")
                $isCanonicalLane = $lower.Contains($resolvedGateway) -or
                    (-not [string]::IsNullOrWhiteSpace($resolvedConfig) -and $lower.Contains($resolvedConfig)) -or
                    (-not [string]::IsNullOrWhiteSpace($resolvedInstallRoot) -and $lower.Contains($resolvedInstallRoot))
                $isIbcGateway -and $isCanonicalLane
            }
        }
}

function Get-GatewayProcesses {
    $directGateway = @(Get-Process -Name "ibgateway", "ibgateway1" -ErrorAction SilentlyContinue)
    $ibcManagedGateway = @(
        Get-IbcManagedGatewayProcesses `
            -GatewayInstallDir $GatewayDir `
            -ConfigPath $IbcConfigPath `
            -InstallRoot $IbcInstallRoot
    )

    return @($directGateway + $ibcManagedGateway)
}

function Get-ApiListener {
    Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Get-ProcessIdValue {
    param($Process)
    if ($null -ne $Process.Id) {
        return $Process.Id
    }
    return $Process.ProcessId
}

function New-StartLock {
    param(
        [string]$Path,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            $payload = [System.Text.Encoding]::UTF8.GetBytes(
                "pid=$PID started=$((Get-Date).ToUniversalTime().ToString("o"))`n"
            )
            $stream.Write($payload, 0, $payload.Length)
            $stream.Flush()
            return $stream
        } catch [System.IO.IOException] {
            Start-Sleep -Milliseconds 500
        }
    }

    throw "Gateway start lock is already held: $Path"
}

function Wait-NoGatewayProcesses {
    param([int]$TimeoutSeconds = 60)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $remaining = @(Get-GatewayProcesses)
        if ($remaining.Count -eq 0) {
            return $true
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Wait-NoApiListener {
    param([int]$TimeoutSeconds = 60)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (-not (Get-ApiListener)) {
            return $true
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Wait-ApiListener {
    param([int]$TimeoutSeconds = 600)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $listener = Get-ApiListener
        if ($listener) {
            Write-LogLine "gateway API listener ready port=$ApiPort pid=$($listener.OwningProcess)"
            return $listener
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    return $null
}

function Get-ListenerHealthState {
    param([string]$Path)

    if (-not $Path) {
        return "unknown"
    }

    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            return "unknown"
        }
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return "unknown"
        }
        $status = $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return "unknown"
    }

    if ($null -eq $status) {
        return "unknown"
    }

    $details = $status.details
    $handshakeOk = $false
    if ($null -ne $details -and $null -ne $details.handshake_ok) {
        $handshakeOk = [bool]$details.handshake_ok
    }

    if ([bool]$status.healthy -or $handshakeOk) {
        return "healthy"
    }

    $hasFailureSignal = ($status.PSObject.Properties.Name -contains "healthy") -or `
        ($status.PSObject.Properties.Name -contains "consecutive_failures") -or `
        (($null -ne $details) -and ($details.PSObject.Properties.Name -contains "handshake_ok"))
    if ($hasFailureSignal) {
        return "unhealthy"
    }

    return "unknown"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$resolvedGatewayDir = Resolve-GatewayDir -RequestedPath $GatewayDir
if ((Normalize-PathString -Path $resolvedGatewayDir) -ne (Normalize-PathString -Path $GatewayDir)) {
    Write-LogLine "requested gateway dir missing; resolved to installed gateway dir: $resolvedGatewayDir"
}
$GatewayDir = $resolvedGatewayDir

$exe = Resolve-GatewayExecutablePath -GatewayInstallDir $GatewayDir
if (-not (Test-Path -LiteralPath $exe)) {
    Write-LogLine "missing executable: $exe"
    throw "Missing IB Gateway executable: $exe"
}

$lockPath = Join-Path $StateDir "ibgateway_start.lock"
$lockStream = $null

try {
    $lockStream = New-StartLock -Path $lockPath

    $listener = Get-ApiListener
    $listenerHealth = if ($listener) {
        Get-ListenerHealthState -Path $HealthStatusPath
    } else {
        "missing"
    }
    if ($listener -and $ForceRestart -and -not $AllowHealthyRestart) {
        if ($listenerHealth -eq "healthy") {
            Write-LogLine "ForceRestart requested but healthy API listener is already present port=$ApiPort pid=$($listener.OwningProcess); skipping restart to preserve authenticated IBKR session"
            return
        }
        Write-LogLine "ForceRestart requested and listener is present but watchdog health is $listenerHealth; proceeding with restart port=$ApiPort pid=$($listener.OwningProcess)"
    }

    if ($listener -and -not $ForceRestart) {
        if ($listenerHealth -eq "unhealthy") {
            throw "Gateway listener exists on port $ApiPort but watchdog health is unhealthy; rerun with -ForceRestart"
        }
        Write-LogLine "existing gateway process running; no start needed port=$ApiPort pid=$($listener.OwningProcess) health=$listenerHealth"
        return
    }

    $existingGateway = @(Get-GatewayProcesses)
    if ($existingGateway.Count -gt 0 -and -not $ForceRestart) {
        $pids = ($existingGateway | ForEach-Object { Get-ProcessIdValue $_ }) -join ","
        Write-LogLine "gateway process running without API listener; waiting for port=$ApiPort pids=$pids"
        $listener = Wait-ApiListener -TimeoutSeconds ([Math]::Min(60, $StartupTimeoutSeconds))
        if ($listener) {
            return
        }
        throw "Gateway process exists but API port $ApiPort did not become ready. pids=$pids"
    }

    if ($ForceRestart) {
        $existingGatewayPids = @($existingGateway | ForEach-Object { Get-ProcessIdValue $_ })
        foreach ($proc in $existingGateway) {
            $procId = Get-ProcessIdValue $proc
            Write-LogLine "stopping existing gateway process pid=$procId"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
        if ($listener) {
            $listenerPid = [int]$listener.OwningProcess
            if ($listenerPid -gt 0 -and ($existingGatewayPids -notcontains $listenerPid)) {
                Write-LogLine "stopping existing gateway API listener owner pid=$listenerPid"
                Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue
            }
        }
        if (-not (Wait-NoGatewayProcesses -TimeoutSeconds 60)) {
            $remaining = @(Get-GatewayProcesses | ForEach-Object { Get-ProcessIdValue $_ }) -join ","
            throw "Timed out waiting for IB Gateway processes to exit. remaining=$remaining"
        }
        if (-not (Wait-NoApiListener -TimeoutSeconds 60)) {
            $remainingListener = Get-ApiListener
            throw "Timed out waiting for IB Gateway API listener to exit. remaining_pid=$($remainingListener.OwningProcess)"
        }
    }

    if ($UseIbc) {
        $ibcInstallDir = Resolve-IbcInstallDir -InstallRoot $IbcInstallRoot -StatePath $IbcInstallStatePath
        $ibcStarter = Join-Path $ibcInstallDir "scripts\StartIBC.bat"
        if (-not (Test-Path -LiteralPath $ibcStarter)) {
            throw "Missing IBC starter: $ibcStarter"
        }

        $ibcCreds = Resolve-IbcCredentials `
            -ExplicitUserId $IbcUserId `
            -ExplicitPassword $IbcPassword `
            -PasswordFile $IbcPasswordFile `
            -CredentialJsonPath $IbcCredentialJsonPath `
            -DefaultUserId $LoginProfile
        if ([string]::IsNullOrWhiteSpace($ibcCreds.user_id) -or [string]::IsNullOrWhiteSpace($ibcCreds.password)) {
            throw "IBC launch requires IBKR credentials. Seed ETA_IBC_LOGIN_ID and ETA_IBC_PASSWORD (or pass -IbcUserId / -LoginProfile / -IbcPassword / -IbcPasswordFile) before using -UseIbc."
        }

        Write-IbcConfig `
            -Path $IbcConfigPath `
            -UserId $ibcCreds.user_id `
            -Password $ibcCreds.password `
            -TradingMode $IbcTradingMode `
            -TwoFactorTimeoutAction $IbcTwoFactorTimeoutAction `
            -AutoRestartTime $IbcAutoRestartTime `
            -ExistingSessionDetectedAction $IbcExistingSessionDetectedAction `
            -AcceptIncomingConnectionAction $IbcAcceptIncomingConnectionAction `
            -LoginDialogDisplayTimeoutSeconds $IbcLoginDialogDisplayTimeoutSeconds `
            -MinimizeMainWindow:$IbcMinimizeMainWindow

        Write-LogLine "IBC session ownership action=$IbcExistingSessionDetectedAction api_port=$ApiPort"
        $gatewayVersion = Split-Path -Leaf $GatewayDir
        $twsRootDir = Resolve-TwsRootDir -ResolvedGatewayDir $GatewayDir
        $arguments = @(
            $gatewayVersion,
            "/Gateway",
            "/IbcPath:$ibcInstallDir",
            "/Config:$IbcConfigPath",
            "/TwsPath:$twsRootDir",
            "/TwsSettingsPath:$GatewayDir",
            "/Mode:$($IbcTradingMode.ToLowerInvariant())",
            "/On2FATimeout:$($IbcTwoFactorTimeoutAction.ToLowerInvariant())"
        )
        Write-LogLine "starting IBC gateway via $ibcStarter $gatewayVersion /Gateway /IbcPath:$ibcInstallDir /Config:$IbcConfigPath /TwsPath:$twsRootDir /TwsSettingsPath:$GatewayDir /Mode:$($IbcTradingMode.ToLowerInvariant()) /On2FATimeout:$($IbcTwoFactorTimeoutAction.ToLowerInvariant())"
        $started = Start-Process -FilePath $ibcStarter `
            -ArgumentList $arguments `
            -WorkingDirectory (Split-Path -Parent $ibcStarter) `
            -PassThru
    } else {
        $arguments = "-login=$LoginProfile"
        if ((Split-Path -Leaf $exe).Equals("ibgateway1.exe", [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-LogLine "direct launcher is using ibgateway1.exe because IBC has renamed the canonical executable in $GatewayDir"
        }
        Write-LogLine "starting $exe $arguments"
        $started = Start-Process -FilePath $exe `
            -ArgumentList $arguments `
            -WorkingDirectory $GatewayDir `
            -PassThru
    }

    Write-LogLine "start requested pid=$(Get-ProcessIdValue $started)"
    $listener = Wait-ApiListener -TimeoutSeconds $StartupTimeoutSeconds
    if (-not $listener) {
        throw "IB Gateway did not open API port $ApiPort within $StartupTimeoutSeconds seconds"
    }
} catch {
    Write-LogLine "ERROR $($_.Exception.Message)"
    throw
} finally {
    if ($null -ne $lockStream) {
        $lockStream.Close()
        $lockStream.Dispose()
    }
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
