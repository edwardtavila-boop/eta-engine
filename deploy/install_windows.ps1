# ============================================================================
# APEX PREDATOR // install_windows.ps1
# ----------------------------------------------------------------------------
# Idempotent Windows Server installer for the Apex Predator stack.
# Windows-native equivalent of install_vps.sh -- uses Task Scheduler instead
# of cron, NSSM/pythonw instead of systemd.
#
# Safe to re-run.
#
# Usage on the VPS (as the operator user, any PowerShell):
#   cd C:\apex_predator
#   powershell -ExecutionPolicy Bypass -File .\deploy\install_windows.ps1
#
# Or from a remote shell:
#   powershell -ExecutionPolicy Bypass -File C:\apex_predator\deploy\install_windows.ps1 -RepoUrl https://github.com/you/apex_predator.git
#
# What it does:
#   1. Verifies prereqs (Python 3.12, Git)
#   2. Clones/pulls the repo to -InstallDir (default C:\apex_predator)
#   3. Creates .venv + installs deps
#   4. Writes .env from .env.example (if missing, appends Claude layer stanza)
#   5. Runs the test suite -- aborts on failure
#   6. Registers Task Scheduler entries for the 12 Avengers cron tasks
#   7. Registers 3 boot-time tasks for jarvis-live / avengers-fleet / dashboard
#   8. Prints post-install checklist
# ============================================================================
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\apex_predator",
    [string]$RepoUrl = "https://github.com/edwardtavila-boop/apex_predator.git",
    [string]$Branch = "main",
    [switch]$SkipTests,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Write-Log   { param($msg) Write-Host "[apex-install] $msg" -ForegroundColor Cyan }
function Write-OK    { param($msg) Write-Host "[ OK ] $msg"        -ForegroundColor Green }
function Write-Warn2 { param($msg) Write-Host "[WARN] $msg"        -ForegroundColor Yellow }
function Die         { param($msg) Write-Host "[FATAL] $msg"       -ForegroundColor Red; exit 1 }

if ($DryRun) { Write-Log "DRY RUN -- will print intended actions only" }

# ----------------------------------------------------------------------------
# 1. Prerequisites
# ----------------------------------------------------------------------------
Write-Log "Step 1/8 -- checking prerequisites"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Die "python not found in PATH" }
$pyVer = (& python --version 2>&1).ToString()
if ($pyVer -notmatch "3\.1[2-9]") { Die "need Python 3.12+, got $pyVer" }
Write-OK "python = $pyVer"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) { Die "git not found in PATH" }
Write-OK "git    = $((& git --version))"

# ----------------------------------------------------------------------------
# 2. Clone / pull
# ----------------------------------------------------------------------------
Write-Log "Step 2/8 -- repo at $InstallDir"
if (Test-Path (Join-Path $InstallDir ".git")) {
    if (-not $DryRun) {
        Push-Location $InstallDir
        git fetch --all
        git checkout $Branch
        git pull --ff-only
        Pop-Location
    }
    Write-OK "repo updated"
} elseif ($RepoUrl) {
    if (-not $DryRun) {
        git clone $RepoUrl $InstallDir -b $Branch
    }
    Write-OK "repo cloned"
} else {
    Die "No repo at $InstallDir and no -RepoUrl"
}
Set-Location $InstallDir

# ----------------------------------------------------------------------------
# 3. Virtualenv + dependencies
# ----------------------------------------------------------------------------
Write-Log "Step 3/8 -- virtualenv + dependencies"
$venvPath = Join-Path $InstallDir ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    if (-not $DryRun) {
        python -m venv $venvPath
    }
    Write-OK "created .venv"
}
if (-not $DryRun) {
    & $venvPython -m pip install --upgrade pip wheel setuptools 2>&1 | Out-Null
    & $venvPython -m pip install -e ".[dev]" anthropic 2>&1 | Out-Null
}
Write-OK "dependencies installed"

# ----------------------------------------------------------------------------
# 4. .env file
# ----------------------------------------------------------------------------
Write-Log "Step 4/8 -- .env file"
$envPath = Join-Path $InstallDir ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $InstallDir ".env.example") $envPath
    Write-OK "wrote .env from .env.example"
    Write-Warn2 "FILL IN REAL VALUES in .env before starting services"
} else {
    Write-OK ".env exists (not touching)"
}

$envContent = Get-Content $envPath -Raw
if ($envContent -notmatch "ANTHROPIC_API_KEY=") {
    @"


# ---------------------------------------------------------------------------
# claude_layer / Avengers (appended by install_windows.ps1)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY=
JARVIS_HOURLY_USD_BUDGET=1.00
JARVIS_DAILY_USD_BUDGET=10.00
JARVIS_DISTILL_SKIP_THRESHOLD=0.92
"@ | Add-Content $envPath
    Write-OK "appended claude_layer stanza to .env"
}

# ----------------------------------------------------------------------------
# 5. State + log directories
# ----------------------------------------------------------------------------
$stateDir = Join-Path $env:LOCALAPPDATA "apex_predator\state"
$logDir   = Join-Path $env:LOCALAPPDATA "apex_predator\logs"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Write-OK "state + log dirs ready"

# ----------------------------------------------------------------------------
# 6. Tests
# ----------------------------------------------------------------------------
if ($SkipTests) {
    Write-Warn2 "skipping tests (-SkipTests)"
} else {
    Write-Log "Step 5/8 -- running test suite"
    if (-not $DryRun) {
        & $venvPython -m pytest tests/ -q --tb=line -x
        if ($LASTEXITCODE -ne 0) { Die "test suite failed" }
    }
    Write-OK "all tests green"
}

# ----------------------------------------------------------------------------
# 7. Task Scheduler -- scheduled cron-equivalent tasks
# ----------------------------------------------------------------------------
Write-Log "Step 6/8 -- registering 12 scheduled tasks"

# Task definitions: (TaskName, TriggerFactory, BackgroundTaskName)
$scheduledTasks = @(
    # ROBIN
    @{ Name = "Apex-Robin-DashboardAssemble";  Task = "DASHBOARD_ASSEMBLE"; Trigger = "MINUTELY" },
    @{ Name = "Apex-Robin-LogCompact";         Task = "LOG_COMPACT";        Trigger = "HOURLY" },
    @{ Name = "Apex-Robin-PromptWarmup";       Task = "PROMPT_WARMUP";      Trigger = "DAILY-1325" },
    @{ Name = "Apex-Robin-AuditSummarize";     Task = "AUDIT_SUMMARIZE";    Trigger = "DAILY-0600" },
    # ALFRED
    @{ Name = "Apex-Alfred-ShadowTick";        Task = "SHADOW_TICK";        Trigger = "EVERY-5MIN" },
    @{ Name = "Apex-Alfred-DriftSummary";      Task = "DRIFT_SUMMARY";      Trigger = "EVERY-15MIN" },
    @{ Name = "Apex-Alfred-KaizenRetro";       Task = "KAIZEN_RETRO";       Trigger = "DAILY-2300" },
    @{ Name = "Apex-Alfred-DistillTrain";      Task = "DISTILL_TRAIN";      Trigger = "WEEKLY-SUN-0200" },
    # BATMAN
    @{ Name = "Apex-Batman-TwinVerdict";       Task = "TWIN_VERDICT";       Trigger = "DAILY-2200" },
    @{ Name = "Apex-Batman-StrategyMine";      Task = "STRATEGY_MINE";      Trigger = "WEEKLY-MON-0300" },
    @{ Name = "Apex-Batman-CausalReview";      Task = "CAUSAL_REVIEW";      Trigger = "MONTHLY-0400" },
    @{ Name = "Apex-Batman-DoctrineReview";    Task = "DOCTRINE_REVIEW";    Trigger = "QUARTERLY-0500" }
)

function New-ApexTrigger {
    param([string]$Spec)
    switch -Regex ($Spec) {
        "^MINUTELY$"          { return New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration ([TimeSpan]::MaxValue) }
        "^EVERY-5MIN$"        { return New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue) }
        "^EVERY-15MIN$"       { return New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue) }
        "^HOURLY$"            { return New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)   -RepetitionDuration ([TimeSpan]::MaxValue) }
        "^DAILY-(\d{2})(\d{2})$" {
            $h = [int]$matches[1]; $m = [int]$matches[2]
            return New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour $h -Minute $m -Second 0)
        }
        "^WEEKLY-(\w+)-(\d{2})(\d{2})$" {
            $dow = $matches[1]; $h = [int]$matches[2]; $m = [int]$matches[3]
            $dowMap = @{ "SUN"="Sunday"; "MON"="Monday"; "TUE"="Tuesday"; "WED"="Wednesday"; "THU"="Thursday"; "FRI"="Friday"; "SAT"="Saturday" }
            return New-ScheduledTaskTrigger -Weekly -DaysOfWeek $dowMap[$dow] -At (Get-Date -Hour $h -Minute $m -Second 0)
        }
        "^MONTHLY-(\d{2})(\d{2})$" {
            $h = [int]$matches[1]; $m = [int]$matches[2]
            # Task Scheduler doesn't have a Monthly option in New-ScheduledTaskTrigger;
            # approximate with daily + a script guard. Good enough for v1.
            return New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour $h -Minute $m -Second 0)
        }
        "^QUARTERLY-(\d{2})(\d{2})$" {
            $h = [int]$matches[1]; $m = [int]$matches[2]
            return New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour $h -Minute $m -Second 0)
        }
    }
    throw "unknown trigger spec: $Spec"
}

foreach ($t in $scheduledTasks) {
    $taskName = $t.Name
    $bgTask   = $t.Task
    if (-not $DryRun) {
        # Idempotent: remove existing with same name first
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        $action = New-ScheduledTaskAction -Execute $venvPython `
            -Argument "-m deploy.scripts.run_task $bgTask --state-dir `"$stateDir`" --log-dir `"$logDir`"" `
            -WorkingDirectory $InstallDir
        $trigger = New-ApexTrigger -Spec $t.Trigger
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
            -AllowStartIfOnBatteries -RunOnlyIfNetworkAvailable:$false `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null
    }
    Write-OK "task registered: $taskName ($bgTask)"
}

# ----------------------------------------------------------------------------
# 8. Boot-time services (JARVIS live + Avengers daemon + Dashboard)
# ----------------------------------------------------------------------------
Write-Log "Step 7/8 -- boot-time tasks (jarvis-live, avengers-fleet, dashboard)"

$bootTasks = @(
    @{ Name = "Apex-Jarvis-Live";       Script = "apex_predator.scripts.jarvis_live"; Args = "--inputs docs\premarket_inputs.json --out-dir `"$stateDir`" --interval 60" },
    @{ Name = "Apex-Avengers-Fleet";    Script = "deploy.scripts.avengers_daemon";    Args = "--state-dir `"$stateDir`" --log-dir `"$logDir`"" },
    @{ Name = "Apex-Dashboard";         Script = "uvicorn";                            Args = "apex_predator.main:app --host 127.0.0.1 --port 8000" }
)

foreach ($t in $bootTasks) {
    if (-not $DryRun) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
        $action = New-ScheduledTaskAction -Execute $venvPython `
            -Argument "-m $($t.Script) $($t.Args)" -WorkingDirectory $InstallDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
            -AllowStartIfOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
            -Settings $settings -User $env:USERNAME -RunLevel Limited | Out-Null
    }
    Write-OK "boot task registered: $($t.Name)"
}

# ----------------------------------------------------------------------------
# 9. Post-install checklist
# ----------------------------------------------------------------------------
Write-Log "Step 8/8 -- DONE. Next steps:"
@"

  1. Edit secrets:
       notepad $InstallDir\.env
     (TRADOVATE_*, ANTHROPIC_API_KEY, any other credentials)

  2. Smoke-check the install:
       $venvPython -m deploy.scripts.smoke_check --skip-systemd

  3. Start the boot tasks manually for the first time:
       Start-ScheduledTask -TaskName "Apex-Jarvis-Live"
       Start-ScheduledTask -TaskName "Apex-Avengers-Fleet"
       Start-ScheduledTask -TaskName "Apex-Dashboard"

  4. View logs:
       Get-Content "$logDir\jarvis-live.log" -Tail 50 -Wait
       Get-Content "$logDir\avengers-fleet.log" -Tail 50 -Wait

  5. Open the dashboard (tunnel / reverse-proxy as needed):
       http://127.0.0.1:8000

  6. View / manage tasks:
       Get-ScheduledTask -TaskName "Apex-*" | Format-Table TaskName, State
       # Disable a task:  Disable-ScheduledTask -TaskName Apex-...
       # Stop a task:     Stop-ScheduledTask -TaskName Apex-...

"@ | Write-Host
Write-OK "install complete"
