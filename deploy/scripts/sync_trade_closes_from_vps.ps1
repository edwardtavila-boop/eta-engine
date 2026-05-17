[CmdletBinding()]
param(
    [string]$SshHost = "",
    [string]$SshUser = "",
    [string]$SshKeyPath = "",
    [string]$RemoteRepoRoot = "C:\EvolutionaryTradingAlgo",
    [switch]$SkipRefresh,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Setting {
    param(
        [AllowEmptyString()]
        [AllowNull()][string]$Value,
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)][string]$EnvName,
        [AllowEmptyString()]
        [Parameter(Mandatory = $true)][string]$DefaultValue
    )

    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        return $Value.Trim()
    }

    $envValue = [Environment]::GetEnvironmentVariable($EnvName)
    if (-not [string]::IsNullOrWhiteSpace($envValue)) {
        return $envValue.Trim()
    }

    return $DefaultValue
}

function Get-SshArgs {
    param(
        [Parameter(Mandatory = $true)][string]$TargetHost,
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $false)][string]$KeyPath
    )

    $args = @(
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes"
    )

    if (-not [string]::IsNullOrWhiteSpace($KeyPath)) {
        $args += @("-i", $KeyPath)
    }

    return $args
}

function Copy-FromRemote {
    param(
        [Parameter(Mandatory = $true)][string]$TargetHost,
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $false)][string]$KeyPath,
        [Parameter(Mandatory = $true)][string]$RemotePath,
        [Parameter(Mandatory = $true)][string]$LocalPath
    )

    $args = Get-SshArgs -TargetHost $TargetHost -User $User -KeyPath $KeyPath
    $args += ("{0}@{1}:{2}" -f $User, $TargetHost, $RemotePath)
    $args += $LocalPath
    & scp @args
    if ($LASTEXITCODE -ne 0) {
        throw "scp failed for $RemotePath"
    }
}

function Resolve-PythonExe {
    $explicit = [Environment]::GetEnvironmentVariable("ETA_PYTHON_EXE")
    if (-not [string]::IsNullOrWhiteSpace($explicit)) {
        return $explicit.Trim()
    }

    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    throw "Unable to locate python. Set ETA_PYTHON_EXE or install python on PATH."
}

function Invoke-RefreshModule {
    param(
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$ModuleName,
        [Parameter(Mandatory = $false)][bool]$AllowNonZeroExit = $false
    )

    Write-Host ("   - {0}" -f $ModuleName)
    Push-Location $WorkspaceRoot
    try {
        & $PythonExe -m $ModuleName --json | Out-Null
        if ($LASTEXITCODE -ne 0 -and -not $AllowNonZeroExit) {
            throw "Refresh module failed: $ModuleName"
        }
        if ($LASTEXITCODE -ne 0 -and $AllowNonZeroExit) {
            Write-Host ("     non-zero verdict preserved for {0} (exit={1})" -f $ModuleName, $LASTEXITCODE)
        }
    }
    finally {
        Pop-Location
    }
}

$WorkspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$SshHost = Resolve-Setting -Value $SshHost -EnvName "FIRM_VPS_HOST" -DefaultValue "93.120.38.156"
$SshUser = Resolve-Setting -Value $SshUser -EnvName "FIRM_VPS_USER" -DefaultValue "codex-admin"
$SshKeyPath = Resolve-Setting -Value $SshKeyPath -EnvName "FIRM_VPS_SSH_KEY_PATH" -DefaultValue ""

$LocalTradeClosesPath = Join-Path $WorkspaceRoot "var\eta_engine\state\jarvis_intel\trade_closes.jsonl"
$RemoteTradeClosesPath = Join-Path $RemoteRepoRoot "var\eta_engine\state\jarvis_intel\trade_closes.jsonl"
$RefreshModules = @(
    @{ Name = "eta_engine.scripts.closed_trade_ledger"; AllowNonZeroExit = $false },
    @{ Name = "eta_engine.scripts.diamond_edge_audit"; AllowNonZeroExit = $false },
    @{ Name = "eta_engine.scripts.diamond_leaderboard"; AllowNonZeroExit = $false },
    @{ Name = "eta_engine.scripts.diamond_retune_status"; AllowNonZeroExit = $false },
    @{ Name = "eta_engine.scripts.diamond_ops_dashboard"; AllowNonZeroExit = $true }
)

Write-Host "=== VPS trade-closes sync ==="
Write-Host "workspace    : $WorkspaceRoot"
Write-Host "target       : $SshUser@$SshHost"
Write-Host "remote source: $RemoteTradeClosesPath"
Write-Host "local target : $LocalTradeClosesPath"
Write-Host "skip refresh : $SkipRefresh"
Write-Host "dry run      : $DryRun"
Write-Host ""

if ($DryRun) {
    Write-Host "[DRY RUN] Would pull authoritative VPS trade closes into the local canonical path."
    foreach ($module in $RefreshModules) {
        Write-Host ("[DRY RUN] Would refresh {0}" -f $module.Name)
    }
    exit 0
}

$localParent = Split-Path -Parent $LocalTradeClosesPath
if (-not (Test-Path -LiteralPath $localParent)) {
    New-Item -ItemType Directory -Force -Path $localParent | Out-Null
}

if (Test-Path -LiteralPath $LocalTradeClosesPath) {
    $backupPath = "{0}.bak_{1}" -f $LocalTradeClosesPath, (Get-Date -Format "yyyyMMdd_HHmmss")
    Copy-Item -LiteralPath $LocalTradeClosesPath -Destination $backupPath -Force
    Write-Host "Backed up existing local canonical trade closes:"
    Write-Host "  $backupPath"
}

Write-Host "[1/2] Pulling canonical trade closes from VPS..."
Copy-FromRemote `
    -TargetHost $SshHost `
    -User $SshUser `
    -KeyPath $SshKeyPath `
    -RemotePath $RemoteTradeClosesPath `
    -LocalPath $LocalTradeClosesPath
Write-Host "Pulled:"
Write-Host "  $RemoteTradeClosesPath"
Write-Host ""

if (-not $SkipRefresh) {
    $pythonExe = Resolve-PythonExe
    Write-Host "[2/2] Refreshing dependent local receipts..."
    foreach ($module in $RefreshModules) {
        Invoke-RefreshModule `
            -WorkspaceRoot $WorkspaceRoot `
            -PythonExe $pythonExe `
            -ModuleName $module.Name `
            -AllowNonZeroExit ([bool]$module.AllowNonZeroExit)
    }
    Write-Host ""
}

$syncedItem = Get-Item -LiteralPath $LocalTradeClosesPath
Write-Host "Synced local canonical trade closes:"
Write-Host ("  length={0} last_write={1:o}" -f $syncedItem.Length, $syncedItem.LastWriteTimeUtc)
Write-Host "=== VPS trade-closes sync complete ==="
