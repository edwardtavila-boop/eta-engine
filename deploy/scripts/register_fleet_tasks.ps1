# EVOLUTIONARY TRADING ALGO // register_fleet_tasks.ps1
# ==========================================
# Register the BTC broker-paper fleet + MNQ live supervisor as
# Windows Scheduled Tasks so they survive VPS reboots.
#
# Pattern matches Apex-Dashboard: runs as S4U principal (no password
# prompt, runs after reboot without a logged-in session), AtStartup
# trigger, per-task argument line in the ScheduledTaskAction.
#
# After running, tasks are visible as:
#   - Apex-BTC-Fleet      (starts btc_broker_fleet --start)
#   - Apex-MNQ-Supervisor (optional; disabled by default -- needs a
#                          bar source on disk)
#
# Usage (run elevated):
#   powershell.exe -ExecutionPolicy Bypass -File `
#     C:\eta_engine\deploy\scripts\register_fleet_tasks.ps1 `
#     -BtcAutoSubmit -McpRoot C:\eta_engine
#
# Idempotent: existing tasks with the same name are updated in place.

[CmdletBinding()]
param(
    [string]$McpRoot = "C:\eta_engine",
    [string]$PythonExe = "C:\eta_engine\.venv\Scripts\python.exe",
    [string]$RunAsUser = "",  # e.g. "trader"; auto-detects from Apex-Dashboard if empty
    [switch]$BtcAutoSubmit,
    [string]$PaperLaneAnchorPrice = "90000",
    [switch]$RegisterMnqSupervisor,
    [string]$MnqBarsPath = ""
)

$ErrorActionPreference = "Stop"

# Auto-detect the principal from an existing Apex-* task so we run
# under the same identity as Apex-Dashboard (the operator-chosen
# runtime account, typically 'trader' on this VPS).
if (-not $RunAsUser) {
    $existingDashboard = Get-ScheduledTask -TaskName "Apex-Dashboard" -ErrorAction SilentlyContinue
    if ($existingDashboard) {
        $RunAsUser = $existingDashboard.Principal.UserId
        Write-Host "Auto-detected RunAsUser from Apex-Dashboard: $RunAsUser"
    } else {
        $RunAsUser = "Administrator"
        Write-Host "No Apex-Dashboard task found; defaulting RunAsUser to Administrator"
    }
}

function Register-ApexTask {
    param(
        [string]$Name,
        [string]$Description,
        [string]$WorkingDir,
        [string]$Executable,
        [string]$Arguments,
        [hashtable]$EnvVars = @{}
    )

    Write-Host "==> Registering task: $Name"

    # Stop + unregister the existing task if any, to make this
    # idempotent without accidentally pointing at the wrong binary.
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "    existing task found; updating..."
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    }

    # Embed env vars into the cmd prefix so child process sees them.
    $envPrefix = ""
    foreach ($key in $EnvVars.Keys) {
        $envPrefix += "set ""$key=$($EnvVars[$key])"" && "
    }

    # Wrap through cmd /c so env vars take effect + we can chain cd.
    $cmdLine = "/c $envPrefix cd /d ""$WorkingDir"" && ""$Executable"" $Arguments"
    $action = New-ScheduledTaskAction `
        -Execute "cmd.exe" `
        -Argument $cmdLine `
        -WorkingDirectory $WorkingDir

    # AtStartup trigger + restart-on-failure
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT30S"   # wait 30s after boot for network/cloudflared

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0)  # 0 = unlimited

    # Match whatever principal Apex-Dashboard uses. 'trader' on this
    # VPS; fallback 'Administrator' for clean boxes. LogonType follows
    # what works for the same account -- Interactive/S4U are both
    # valid depending on operator policy, but auto-detect from the
    # existing Apex-Dashboard task when possible.
    $dashPrincipal = (
        Get-ScheduledTask -TaskName "Apex-Dashboard" -ErrorAction SilentlyContinue
    )
    if ($dashPrincipal) {
        $logonType = $dashPrincipal.Principal.LogonType
        $runLevel = $dashPrincipal.Principal.RunLevel
    } else {
        $logonType = "S4U"
        $runLevel = "Highest"
    }
    $principal = New-ScheduledTaskPrincipal `
        -UserId $RunAsUser `
        -LogonType $logonType `
        -RunLevel $runLevel

    Register-ScheduledTask `
        -TaskName $Name `
        -Description $Description `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null

    Write-Host "    OK"
}

# ---------------------------------------------------------------------------
# BTC fleet task
# ---------------------------------------------------------------------------
$btcEnv = @{
    "BTC_PAPER_LANE_ANCHOR_PRICE" = $PaperLaneAnchorPrice
}
if ($BtcAutoSubmit) {
    $btcEnv["BTC_PAPER_LANE_AUTO_SUBMIT"] = "1"
}

Register-ApexTask `
    -Name "Apex-BTC-Fleet" `
    -Description "BTC broker-paper fleet (4 lanes: directional/grid x tastytrade/ibkr). Survives reboots via AtStartup trigger." `
    -WorkingDir $McpRoot `
    -Executable $PythonExe `
    -Arguments "-m eta_engine.scripts.btc_broker_fleet --start" `
    -EnvVars $btcEnv

# ---------------------------------------------------------------------------
# MNQ supervisor task (optional -- only register with --RegisterMnqSupervisor)
# ---------------------------------------------------------------------------
if ($RegisterMnqSupervisor) {
    if (-not $MnqBarsPath) {
        Write-Host "ERROR: -RegisterMnqSupervisor requires -MnqBarsPath <file.jsonl>"
        exit 2
    }
    if (-not (Test-Path $MnqBarsPath)) {
        Write-Host "WARNING: MnqBarsPath does not exist: $MnqBarsPath"
        Write-Host "         task will be registered but will exit until the file is created."
    }
    Register-ApexTask `
        -Name "Apex-MNQ-Supervisor" `
        -Description "MNQ live supervisor -- drives MnqBot through a JSONL bar stream with JARVIS + IBKR paper routing." `
        -WorkingDir $McpRoot `
        -Executable $PythonExe `
        -Arguments "-m eta_engine.scripts.mnq_live_supervisor --bars ""$MnqBarsPath"""
}

Write-Host ""
Write-Host "Done. Verify with:"
Write-Host "  Get-ScheduledTask -TaskName 'Apex-*' | Select-Object TaskName, State"
