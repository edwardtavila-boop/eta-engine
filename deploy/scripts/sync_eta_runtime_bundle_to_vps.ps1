[CmdletBinding()]
param(
    [string]$SshHost = "",
    [string]$SshUser = "",
    [string]$SshKeyPath = "",
    [string]$RemoteRepoRoot = "C:\EvolutionaryTradingAlgo",
    [string[]]$Files = @(
        "eta_engine\scripts\alert_channel_config.py",
        "eta_engine\scripts\closed_trade_ledger.py",
        "eta_engine\scripts\diamond_artifact_surface_check.py",
        "eta_engine\scripts\diamond_prop_alert_dispatcher.py",
        "eta_engine\scripts\diamond_prop_prelaunch_dryrun.py",
        "eta_engine\scripts\diamond_retune_truth_check.py",
        "eta_engine\scripts\health_check.py",
        "eta_engine\scripts\jarvis_strategy_supervisor.py",
        "eta_engine\scripts\project_kaizen_closeout.py",
        "eta_engine\scripts\prop_launch_check.py",
        "eta_engine\scripts\retune_advisory_cache.py",
        "eta_engine\scripts\supervisor_heartbeat_check.py",
        "eta_engine\scripts\verify_telegram.py",
        "eta_engine\scripts\workspace_roots.py",
        "eta_engine\strategies\per_bot_registry.py"
    ),
    [switch]$SkipVerify,
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

function New-RemotePowerShellCommand {
    param([Parameter(Mandatory = $true)][string]$Command)

    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    return @(
        "powershell",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        $encoded
    ) -join " "
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

    $args += "$User@$TargetHost"
    return $args
}

function Invoke-Remote {
    param(
        [Parameter(Mandatory = $true)][string]$TargetHost,
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $false)][string]$KeyPath,
        [Parameter(Mandatory = $true)][string]$RemoteCommand
    )

    $args = Get-SshArgs -TargetHost $TargetHost -User $User -KeyPath $KeyPath
    $args += $RemoteCommand
    & ssh @args
    if ($LASTEXITCODE -ne 0) {
        throw "ssh command failed with exit code $LASTEXITCODE"
    }
}

function Copy-ToRemote {
    param(
        [Parameter(Mandatory = $true)][string]$TargetHost,
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $false)][string]$KeyPath,
        [Parameter(Mandatory = $true)][string]$LocalPath,
        [Parameter(Mandatory = $true)][string]$RemotePath
    )

    $args = @(
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes"
    )

    if (-not [string]::IsNullOrWhiteSpace($KeyPath)) {
        $args += @("-i", $KeyPath)
    }

    $args += $LocalPath
    $args += ("{0}@{1}:{2}" -f $User, $TargetHost, $RemotePath)
    & scp @args
    if ($LASTEXITCODE -ne 0) {
        throw "scp failed for $LocalPath"
    }
}

$WorkspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$SshHost = Resolve-Setting -Value $SshHost -EnvName "FIRM_VPS_HOST" -DefaultValue "93.120.38.156"
$SshUser = Resolve-Setting -Value $SshUser -EnvName "FIRM_VPS_USER" -DefaultValue "codex-admin"
$SshKeyPath = Resolve-Setting -Value $SshKeyPath -EnvName "FIRM_VPS_SSH_KEY_PATH" -DefaultValue ""
$RemoteBackupRoot = Join-Path $RemoteRepoRoot "var\eta_engine\state\codex_sync_backups\eta_runtime_bundle"

Write-Host "=== ETA runtime bundle sync ==="
Write-Host "workspace   : $WorkspaceRoot"
Write-Host "target      : $SshUser@$SshHost"
Write-Host "remote root : $RemoteRepoRoot"
Write-Host "backup root : $RemoteBackupRoot"
Write-Host "file count  : $($Files.Count)"
Write-Host "skip verify : $SkipVerify"
Write-Host "dry run     : $DryRun"
Write-Host ""

$FileSpecs = foreach ($relative in $Files) {
    $localPath = Join-Path $WorkspaceRoot $relative
    if (-not (Test-Path -LiteralPath $localPath)) {
        throw "Missing local file: $localPath"
    }

    [pscustomobject]@{
        Relative   = $relative
        LocalPath  = $localPath
        RemotePath = Join-Path $RemoteRepoRoot $relative
    }
}

if ($DryRun) {
    foreach ($spec in $FileSpecs) {
        Write-Host "[DRY RUN] $($spec.Relative)"
        Write-Host "          local : $($spec.LocalPath)"
        Write-Host "          remote: $($spec.RemotePath)"
    }
    exit 0
}

$backupTs = Get-Date -Format "yyyyMMdd_HHmmss"
$remoteBackupLines = @(
    '$ErrorActionPreference = ''Stop'''
    ('$remoteRoot = ''{0}''' -f $RemoteRepoRoot)
    ('$backupRoot = ''{0}''' -f $RemoteBackupRoot)
    ('$files = @(' + (($FileSpecs | ForEach-Object { "'{0}'" -f $_.RemotePath }) -join ', ') + ')')
    ('$backupTs = ''{0}''' -f $backupTs)
    '$backupBase = Join-Path $backupRoot $backupTs'
    'foreach ($file in $files) {'
    '  $parent = Split-Path -Parent $file'
    '  if (-not [string]::IsNullOrWhiteSpace($parent)) {'
    '    New-Item -ItemType Directory -Force -Path $parent | Out-Null'
    '  }'
    '  if (Test-Path -LiteralPath $file) {'
    '    $relative = $file'
    '    if ($relative.StartsWith($remoteRoot, [System.StringComparison]::OrdinalIgnoreCase)) {'
    '      $relative = $relative.Substring($remoteRoot.Length).TrimStart(''\'')'
    '    }'
    '    $backupPath = Join-Path $backupBase $relative'
    '    $backupParent = Split-Path -Parent $backupPath'
    '    if (-not [string]::IsNullOrWhiteSpace($backupParent)) {'
    '      New-Item -ItemType Directory -Force -Path $backupParent | Out-Null'
    '    }'
    '    Copy-Item -LiteralPath $file -Destination $backupPath -Force'
    '    Write-Output (''BACKED|'' + $file + ''|'' + $backupPath)'
    '  } else {'
    '    Write-Output (''MISSING|'' + $file)'
    '  }'
    '}'
)
$remoteBackupCommand = New-RemotePowerShellCommand -Command ($remoteBackupLines -join "`n")

Write-Host "[1/3] Backing up remote files..."
Invoke-Remote -TargetHost $SshHost -User $SshUser -KeyPath $SshKeyPath -RemoteCommand $remoteBackupCommand
Write-Host ""

Write-Host "[2/3] Copying bundle..."
foreach ($spec in $FileSpecs) {
    Write-Host (" - {0}" -f $spec.Relative)
    Copy-ToRemote -TargetHost $SshHost -User $SshUser -KeyPath $SshKeyPath -LocalPath $spec.LocalPath -RemotePath $spec.RemotePath
}
Write-Host ""

if (-not $SkipVerify) {
    Write-Host "[3/3] Verifying live health/readiness..."
    $verifyLines = @(
        '$ErrorActionPreference = ''Stop'''
        ('[Environment]::SetEnvironmentVariable(''PYTHONPATH'', ''{0}'', ''Process'')' -f $RemoteRepoRoot)
        'python -m eta_engine.scripts.health_check --allow-remote-supervisor-truth --allow-remote-retune-truth'
        'python -m eta_engine.scripts.verify_telegram'
    )
    $verifyCommand = New-RemotePowerShellCommand -Command ($verifyLines -join "`n")
    Invoke-Remote -TargetHost $SshHost -User $SshUser -KeyPath $SshKeyPath -RemoteCommand $verifyCommand
}

Write-Host ""
Write-Host "=== ETA runtime bundle sync complete ==="
