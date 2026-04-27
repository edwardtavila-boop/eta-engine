# ============================================================================
# optimize_vps.ps1 -- Windows Server 2022 tuning for the Evolutionary Trading Algo stack
#
# Does (idempotent, each step reports OK/SKIP):
#   1. Windows Defender exclusions on apex dirs + python/cloudflared
#   3. NTFS: disable 8.3 short-name generation + last-access updates
#   4. All Apex-* tasks -> LogonType=S4U (runs without interactive login)
#   6. Pagefile -> fixed 16 GB (no dynamic resizing stalls)
#
# Run as the operator user (does NOT need Administrator for exclusions via
# Defender API; may need Admin for pagefile + NTFS flags).
# ============================================================================
[CmdletBinding()]
param()

function Log  { param($m) Write-Host "[optimize] $m" -ForegroundColor Cyan }
function OK   { param($m) Write-Host "[ OK ] $m" -ForegroundColor Green }
function Skip { param($m) Write-Host "[SKIP] $m" -ForegroundColor Yellow }
function Warn { param($m) Write-Host "[WARN] $m" -ForegroundColor DarkYellow }
function Die  { param($m) Write-Host "[FAIL] $m" -ForegroundColor Red }

# ----------------------------------------------------------------------------
# #1 -- Windows Defender exclusions
# ----------------------------------------------------------------------------
Log "Step 1/4 -- Defender exclusions"
$paths = @(
    "C:\eta_engine",
    "C:\EvolutionaryTradingAlgo\firm_command_center",
    "$env:LOCALAPPDATA\eta_engine"
)
$processes = @("python.exe", "cloudflared.exe", "pwsh.exe")

foreach ($p in $paths) {
    if (Test-Path $p) {
        try {
            Add-MpPreference -ExclusionPath $p -ErrorAction Stop
            OK "excluded path: $p"
        } catch {
            Warn "could not exclude $p : $($_.Exception.Message)"
        }
    } else {
        Skip "path not found: $p"
    }
}
foreach ($proc in $processes) {
    try {
        Add-MpPreference -ExclusionProcess $proc -ErrorAction Stop
        OK "excluded process: $proc"
    } catch {
        Warn "could not exclude process $proc : $($_.Exception.Message)"
    }
}

# ----------------------------------------------------------------------------
# #3 -- NTFS tuning
# ----------------------------------------------------------------------------
Log "Step 2/4 -- NTFS tuning (8.3 disable + no-last-access)"
try {
    & fsutil behavior set disable8dot3 1 2>&1 | Out-Null
    OK "8.3 short-name generation disabled"
} catch { Warn "disable8dot3: $($_.Exception.Message)" }
try {
    & fsutil behavior set disablelastaccess 1 2>&1 | Out-Null
    OK "disable last-access timestamp updates"
} catch { Warn "disablelastaccess: $($_.Exception.Message)" }

# ----------------------------------------------------------------------------
# #4 -- Logon-independent task execution
# ----------------------------------------------------------------------------
Log "Step 3/4 -- Apex-* tasks -> run whether user logged on or not (S4U)"
$tasks = Get-ScheduledTask -TaskName "Apex-*" -ErrorAction SilentlyContinue
if ($tasks) {
    foreach ($task in $tasks) {
        try {
            # S4U = "Service for User" = runs with user identity without password prompt,
            # survives RDP disconnect, works without interactive session. NetworkAccess
            # is restricted but all our tasks go through HTTP(S) which is fine.
            $principal = New-ScheduledTaskPrincipal `
                -UserId $env:USERNAME `
                -LogonType S4U `
                -RunLevel Limited
            Set-ScheduledTask -TaskName $task.TaskName -Principal $principal | Out-Null
            OK "$($task.TaskName) -> S4U"
        } catch {
            Warn "$($task.TaskName) -> $($_.Exception.Message)"
        }
    }
} else {
    Skip "no Apex-* tasks found"
}

# ----------------------------------------------------------------------------
# #6 -- Fixed pagefile
# ----------------------------------------------------------------------------
Log "Step 4/4 -- Pagefile -> fixed 16 GB on C:"
try {
    $cs = Get-WmiObject Win32_ComputerSystem -EnableAllPrivileges
    if ($cs.AutomaticManagedPagefile) {
        $cs.AutomaticManagedPagefile = $false
        $cs.Put() | Out-Null
        OK "disabled automatic pagefile management"
    } else {
        Skip "automatic pagefile already disabled"
    }

    $existing = Get-WmiObject Win32_PageFileSetting | Where-Object { $_.Name -like "C:\pagefile.sys*" }
    if ($existing) {
        if ($existing.InitialSize -eq 16384 -and $existing.MaximumSize -eq 16384) {
            Skip "pagefile already fixed at 16384 MB"
        } else {
            $existing.InitialSize = 16384
            $existing.MaximumSize = 16384
            $existing.Put() | Out-Null
            OK "pagefile set to fixed 16384 MB (reboot to activate)"
        }
    } else {
        $new = ([WmiClass]"Win32_PageFileSetting").CreateInstance()
        $new.Name = "C:\pagefile.sys"
        $new.InitialSize = 16384
        $new.MaximumSize = 16384
        $new.Put() | Out-Null
        OK "created fixed pagefile (reboot to activate)"
    }
} catch {
    Warn "pagefile tuning needs Administrator: $($_.Exception.Message)"
}

Write-Host ""
Log "optimization pass complete. Reboot VPS to activate pagefile change (the rest are live)."
