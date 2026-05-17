# EVOLUTIONARY TRADING ALGO // disable_non_authoritative_gateway_tasks.ps1
# Disables local Gateway launch/capture tasks on non-authoritative desktop hosts.

[CmdletBinding()]
param(
    [string]$GatewayAuthorityPath = "C:\EvolutionaryTradingAlgo\var\eta_engine\state\gateway_authority.json",
    [switch]$Apply,
    [switch]$StopGatewayProcesses
)

$ErrorActionPreference = "Stop"

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

function Test-GatewayAuthorityMarker {
    param([string]$Path)
    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            return $false
        }
        $payload = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $role = [string]($payload.role)
        $enabled = Convert-GatewayAuthorityEnabled -Value $payload.enabled
        $computer = [string]($payload.computer_name)
        $roleOk = @("vps", "gateway_authority") -contains $role.Trim().ToLowerInvariant()
        $hostOk = [string]::IsNullOrWhiteSpace($computer) -or $computer.Equals($env:COMPUTERNAME, [System.StringComparison]::OrdinalIgnoreCase)
        return ($enabled -and $roleOk -and $hostOk)
    } catch {
        return $false
    }
}

$os = Get-CimInstance Win32_OperatingSystem
$isDesktop = [int]$os.ProductType -eq 1
$isAuthority = Test-GatewayAuthorityMarker -Path $GatewayAuthorityPath

$tasks = @(
    "ETA-IBGateway",
    "ETA-IBGateway-Autostart",
    "ETA-IBGateway-DailyRestart",
    "ETA-IBGateway-RunNow",
    "ETA-IBGateway-Reauth",
    "ETA-TWS-Watchdog",
    "EtaIbkrBbo1mCapture"
)

$result = [ordered]@{
    generated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    computer_name = $env:COMPUTERNAME
    os_caption = [string]$os.Caption
    os_product_type = [int]$os.ProductType
    apply = [bool]$Apply
    gateway_authority = $isAuthority
    desktop_host = $isDesktop
    disabled_tasks = @()
    would_disable_tasks = @()
    skipped_tasks = @()
    stopped_processes = @()
    would_stop_processes = @()
}

if ($isAuthority) {
    $result.status = "skipped_authoritative_host"
    $result.reason = "This host has the Gateway authority marker; not disabling tasks."
    $result | ConvertTo-Json -Depth 5
    exit 0
}

if (-not $isDesktop) {
    $result.status = "skipped_non_desktop_without_marker"
    $result.reason = "Host is not marked authoritative, but is not a workstation; inspect before disabling server tasks."
    $result | ConvertTo-Json -Depth 5
    exit 2
}

foreach ($name in $tasks) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        $result.skipped_tasks += [ordered]@{ task = $name; reason = "missing" }
        continue
    }
    if ($Apply) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Out-Null
    }
    $taskReceipt = [ordered]@{
        task = $name
        previous_state = [string]$task.State
    }
    if ($Apply) {
        $result.disabled_tasks += $taskReceipt
    } else {
        $result.would_disable_tasks += $taskReceipt
    }
}

if ($StopGatewayProcesses) {
    foreach ($proc in @(Get-Process -Name "ibgateway", "ibgateway1" -ErrorAction SilentlyContinue)) {
        $processReceipt = [ordered]@{ name = $proc.ProcessName; pid = $proc.Id }
        if ($Apply) {
            $result.stopped_processes += $processReceipt
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        } else {
            $result.would_stop_processes += $processReceipt
        }
    }
}

$result.status = if ($Apply) { "disabled_local_gateway_tasks" } else { "dry_run" }
$result.reason = "Non-authoritative Windows workstation must not own the ETA IBKR Gateway session."
$result | ConvertTo-Json -Depth 5
