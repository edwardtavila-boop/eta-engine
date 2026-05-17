# EVOLUTIONARY TRADING ALGO // register_tws_watchdog_task.ps1
# Registers the TWS health watchdog frequently enough to satisfy the
# paper-live release guard freshness window.

[CmdletBinding()]
param(
    [string]$TaskName = "ETA-TWS-Watchdog",
    [string]$Root = "C:\EvolutionaryTradingAlgo",
    [string]$PythonExe = "",
    [int]$IntervalSeconds = 60,
    [string]$GatewayAuthorityPath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\gateway_authority.json",
    [switch]$DryRun,
    [switch]$Start,
    [switch]$AllowNonVpsGatewayTaskRegistration
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    if (
        $resolved -ne "C:\EvolutionaryTradingAlgo" -and
        -not $resolved.StartsWith("C:\EvolutionaryTradingAlgo\", [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Refusing non-canonical ETA path: $Path"
    }
    return $resolved
}

function Test-TruthyText {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }
    return @("1", "true", "yes", "y", "vps", "authority") -contains $Value.Trim().ToLowerInvariant()
}

function Convert-GatewayAuthorityEnabled {
    param($Value)
    if ($null -eq $Value) {
        return $false
    }
    if ($Value -is [bool]) {
        return [bool]$Value
    }
    if ($Value -is [System.ValueType]) {
        try {
            return ([double]$Value -ne 0)
        } catch {
            return $false
        }
    }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $false
    }
    return @("1", "true", "yes", "y", "on") -contains $text.Trim().ToLowerInvariant()
}

function Assert-GatewayAuthority {
    param([string]$Path)

    if ($AllowNonVpsGatewayTaskRegistration) {
        Write-Host "WARNING: AllowNonVpsGatewayTaskRegistration accepted on host=$env:COMPUTERNAME" -ForegroundColor Yellow
        return
    }

    if (Test-TruthyText -Value $env:ETA_IBKR_GATEWAY_AUTHORITY) {
        return
    }

    $payload = $null
    if ($Path -and (Test-Path -LiteralPath $Path)) {
        $payload = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    }
    $role = if ($null -ne $payload -and $null -ne $payload.role) { [string]$payload.role } else { "" }
    $enabled = if ($null -ne $payload -and $null -ne $payload.enabled) { Convert-GatewayAuthorityEnabled -Value $payload.enabled } else { $false }
    $markerComputer = if ($null -ne $payload -and $null -ne $payload.computer_name) { [string]$payload.computer_name } else { "" }
    $roleOk = @("vps", "gateway_authority") -contains $role.Trim().ToLowerInvariant()
    $hostOk = [string]::IsNullOrWhiteSpace($markerComputer) -or $markerComputer.Equals($env:COMPUTERNAME, [System.StringComparison]::OrdinalIgnoreCase)
    if ($enabled -and $roleOk -and $hostOk) {
        return
    }

    throw (
        "Refusing to register ETA-TWS-Watchdog on non-authoritative host '$env:COMPUTERNAME'. " +
        "The VPS is the 24/7 IBKR Gateway deployment source. Mark the VPS with " +
        "set_gateway_authority.ps1 -Apply -Role vps."
    )
}

if ($IntervalSeconds -lt 30 -or $IntervalSeconds -gt 120) {
    throw "IntervalSeconds must stay between 30 and 120 so the 180-second release guard cannot flicker stale."
}

$RootFull = Assert-CanonicalEtaPath -Path $Root
$WorkingDir = Assert-CanonicalEtaPath -Path (Join-Path $RootFull "eta_engine")
$StateDir = Assert-CanonicalEtaPath -Path (Join-Path $RootFull "var\eta_engine\state")
$LogDir = Assert-CanonicalEtaPath -Path (Join-Path $RootFull "logs\eta_engine")
$WatchdogScript = Assert-CanonicalEtaPath -Path (Join-Path $WorkingDir "scripts\tws_watchdog.py")
$GatewayAuthorityPath = Assert-CanonicalEtaPath -Path $GatewayAuthorityPath
$VenvPython = Join-Path $WorkingDir ".venv\Scripts\python.exe"
$Python = if ($PythonExe) { $PythonExe } elseif (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python.exe" }

if (-not (Test-Path -LiteralPath $WorkingDir)) {
    throw "Missing canonical ETA engine directory: $WorkingDir"
}
if (-not (Test-Path -LiteralPath $WatchdogScript)) {
    throw "Missing TWS watchdog script: $WatchdogScript"
}

$arguments = "-m eta_engine.scripts.tws_watchdog --host 127.0.0.1 --port 4002 --handshake-attempts 1 --handshake-timeout 30 --skip-account-snapshot"

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Root        : $RootFull"
    Write-Host "  Working dir : $RootFull"
    Write-Host "  Python      : $Python"
    Write-Host "  Arguments   : $arguments"
    Write-Host "  State dir   : $StateDir"
    Write-Host "  Log dir     : $LogDir"
    Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest)"
    Write-Host "  Triggers    : AtStartup + every $IntervalSeconds seconds"
    Write-Host "  Start now   : $($Start.IsPresent)"
    exit 0
}

Assert-GatewayAuthority -Path $GatewayAuthorityPath
New-Item -ItemType Directory -Force -Path $StateDir, $LogDir | Out-Null

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $RootFull
$heartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Seconds $IntervalSeconds) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$triggers = @((New-ScheduledTaskTrigger -AtStartup), $heartbeat)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal `
    -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$description = "ETA TWS API 4002 watchdog. Keeps tws_watchdog.json fresh enough for the 180-second paper-live release guard."
$registeredPrincipal = "SYSTEM"
try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $description `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
} catch {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $fallbackPrincipal = New-ScheduledTaskPrincipal `
        -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $fallbackTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $currentUser), $heartbeat)
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description "$description Current-user fallback because SYSTEM registration was unavailable." `
        -Action $action `
        -Trigger $fallbackTriggers `
        -Settings $settings `
        -Principal $fallbackPrincipal `
        -Force | Out-Null
    $registeredPrincipal = "current_user:$currentUser"
}

Write-Host "OK: Registered '$TaskName' as $registeredPrincipal, startup/logon plus every $IntervalSeconds seconds."
if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "    Started '$TaskName' immediately."
} else {
    Write-Host "    Start now with: Start-ScheduledTask -TaskName '$TaskName'"
}
