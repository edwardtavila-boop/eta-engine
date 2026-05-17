# EVOLUTIONARY TRADING ALGO // register_ibgateway_reauth_task.ps1
# Register a safe IB Gateway recovery controller. This task does not launch
# ibgateway.exe directly; it starts the existing Gateway scheduled tasks so the
# configured Gateway profile and Windows user ownership stay intact.

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start,
    [switch]$CurrentUser,
    [string]$GatewayAuthorityPath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\gateway_authority.json",
    [switch]$AllowNonVpsGatewayTaskRegistration,
    [ValidateRange(30, 300)]
    [int]$IntervalSeconds = 60
)

$ErrorActionPreference = "Stop"

$TaskName = "ETA-IBGateway-Reauth"
$WorkingDir = "C:\EvolutionaryTradingAlgo\eta_engine"
$VenvPython = Join-Path $WorkingDir ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python.exe" }
$StateDir = "C:\EvolutionaryTradingAlgo\var\eta_engine\state"

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
        "Refusing to register ETA-IBGateway-Reauth on non-authoritative host '$env:COMPUTERNAME'. " +
        "The VPS is the 24/7 IBKR Gateway deployment source. Mark the VPS with " +
        "set_gateway_authority.ps1 -Apply -Role vps."
    )
}

if (-not (Test-Path -LiteralPath $WorkingDir)) {
    throw "Missing canonical ETA engine directory: $WorkingDir"
}
$WorkingDir = Assert-CanonicalEtaPath -Path $WorkingDir
$StateDir = Assert-CanonicalEtaPath -Path $StateDir
$GatewayAuthorityPath = Assert-CanonicalEtaPath -Path $GatewayAuthorityPath

$cmdLine = "/c cd /d ""$WorkingDir"" && ""$PythonExe"" -m eta_engine.scripts.ibgateway_reauth_controller --execute"

if ($DryRun) {
    Write-Host "DRY RUN: would register task '$TaskName'"
    Write-Host "  Working dir : $WorkingDir"
    Write-Host "  Python      : $PythonExe"
    Write-Host "  State dir   : $StateDir"
    Write-Host "  Cmd line    : cmd.exe $cmdLine"
    if ($CurrentUser) {
        Write-Host "  Principal   : current user (Interactive/Limited)"
        Write-Host "  Triggers    : AtLogOn + every $IntervalSeconds seconds"
    } else {
        Write-Host "  Principal   : NT AUTHORITY\SYSTEM (Highest), with current-user fallback"
        Write-Host "  Triggers    : AtStartup + every $IntervalSeconds seconds"
    }
    Write-Host "  Start now   : $($Start.IsPresent)"
    exit 0
}

Assert-GatewayAuthority -Path $GatewayAuthorityPath
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Existing '$TaskName' task found; unregistering first."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdLine -WorkingDirectory $WorkingDir
$heartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Seconds $IntervalSeconds) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$triggers = @((New-ScheduledTaskTrigger -AtStartup), $heartbeat)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
$description = "Canonical IB Gateway recovery controller: reads watchdog health and starts the canonical Gateway tasks when safe."
$registeredPrincipal = "SYSTEM"
if ($CurrentUser) {
    $currentUserName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUserName -LogonType Interactive -RunLevel Limited
    $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $currentUserName), $heartbeat)
    $description = "$description Current-user fallback requested by operator."
    $registeredPrincipal = "current_user:$currentUserName"
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
}

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $description `
        -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
        -Force | Out-Null
} catch {
    if ($CurrentUser) {
        throw
    }
    $currentUserName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $fallbackPrincipal = New-ScheduledTaskPrincipal `
        -UserId $currentUserName -LogonType Interactive -RunLevel Limited
    $fallbackTriggers = @((New-ScheduledTaskTrigger -AtLogOn -User $currentUserName), $heartbeat)
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description "$description Current-user fallback because SYSTEM registration was unavailable." `
        -Action $action -Trigger $fallbackTriggers -Settings $settings -Principal $fallbackPrincipal `
        -Force | Out-Null
    $registeredPrincipal = "current_user:$currentUserName"
}

Write-Host "OK: Registered '$TaskName' as $registeredPrincipal, startup/logon plus every $IntervalSeconds seconds."
if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "    Started '$TaskName' immediately."
} else {
    Write-Host "    Start now with:  Start-ScheduledTask -TaskName '$TaskName'"
}
