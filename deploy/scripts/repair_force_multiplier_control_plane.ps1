# Repair ETA Force Multiplier control-plane durability.
#
# This script only repairs the local control-plane surfaces:
# - FmStatusServer WinSW service on 127.0.0.1:8422
# - ETA-ThreeAI-Sync scheduled task via register_codex_operator_task.ps1
# - cached Force Multiplier health and VPS hardening audit artifacts
#
# It never places, cancels, flattens, or promotes broker orders.

[CmdletBinding()]
param(
    [string]$WorkspaceRoot = "C:\EvolutionaryTradingAlgo",
    [switch]$DryRun,
    [switch]$Start,
    [switch]$RestartService
)

$ErrorActionPreference = "Stop"

function Assert-CanonicalEtaPath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
    $canonicalRoot = "C:\EvolutionaryTradingAlgo"
    if (
        $resolved -ne $canonicalRoot -and
        -not $resolved.StartsWith("$canonicalRoot\", [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Refusing non-canonical ETA path: $Path"
    }
}

function Resolve-EtaPython {
    param([string]$EtaEngineRoot)
    $envPython = [Environment]::GetEnvironmentVariable("ETA_PYTHON_EXE", "Machine")
    if (-not $envPython) {
        $envPython = [Environment]::GetEnvironmentVariable("ETA_PYTHON_EXE", "User")
    }
    $venvPython = Join-Path $EtaEngineRoot ".venv\Scripts\python.exe"
    $machinePython = "C:\Python314\python.exe"
    if ($envPython -and (Test-Path -LiteralPath $envPython)) {
        return $envPython
    }
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }
    if (Test-Path -LiteralPath $machinePython) {
        return $machinePython
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    throw "No canonical Python executable found. Expected ETA_PYTHON_EXE, $venvPython, or $machinePython."
}

function Update-FmStatusServiceXmlExecutable {
    param(
        [string]$XmlPath,
        [string]$PythonExe
    )
    $xmlText = Get-Content -LiteralPath $XmlPath -Raw -Encoding UTF8
    $pattern = [regex]::new('<executable>.*?</executable>', [System.Text.RegularExpressions.RegexOptions]::Singleline)
    if (-not $pattern.IsMatch($xmlText)) {
        throw "FmStatusServer XML missing <executable> node: $XmlPath"
    }
    $escapedPython = [Security.SecurityElement]::Escape($PythonExe)
    $updatedText = $pattern.Replace($xmlText, "<executable>$escapedPython</executable>", 1)
    Set-Content -LiteralPath $XmlPath -Value $updatedText -Encoding UTF8
}

function Stop-SafeFmStatusPortOwner {
    param([int]$Port = 8422)
    $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    $owners = @($connections | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique)
    foreach ($pidValue in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
        $commandLine = [string]$proc.CommandLine
        if (
            $commandLine -match "eta_engine\.deploy\.fm_status_server" -or
            $commandLine -match "fm_status_server:app"
        ) {
            Write-Host "Stopping ad-hoc FmStatusServer port owner PID $pidValue before supervised service start." -ForegroundColor Yellow
            Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
        } elseif ($pidValue) {
            Write-Host "Port $Port owner PID $pidValue does not look like FmStatusServer; leaving it untouched." -ForegroundColor Yellow
        }
    }
}

Assert-CanonicalEtaPath -Path $WorkspaceRoot
$EtaEngineRoot = Join-Path $WorkspaceRoot "eta_engine"
$ServicesRoot = Join-Path $WorkspaceRoot "firm_command_center\services"
$StateDir = Join-Path $WorkspaceRoot "var\eta_engine\state"
$LogDir = Join-Path $WorkspaceRoot "logs\eta_engine"
$ServiceName = "FmStatusServer"
$ServiceXmlSource = Join-Path $EtaEngineRoot "deploy\FmStatusServer.xml"
$ServiceXmlTarget = Join-Path $ServicesRoot "$ServiceName.xml"
$WinSWSource = Join-Path $ServicesRoot "winsw.exe"
$ServiceExe = Join-Path $ServicesRoot "$ServiceName.exe"
$LegacyNestedServiceDir = Join-Path $ServicesRoot $ServiceName
$RegisterCodexTasks = Join-Path $EtaEngineRoot "deploy\scripts\register_codex_operator_task.ps1"
$PythonExe = Resolve-EtaPython -EtaEngineRoot $EtaEngineRoot

foreach ($requiredPath in @($EtaEngineRoot, $ServiceXmlSource, $WinSWSource, $RegisterCodexTasks)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required Force Multiplier repair dependency is missing: $requiredPath"
    }
    Assert-CanonicalEtaPath -Path $requiredPath
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Required Force Multiplier repair dependency is missing: $PythonExe"
}

if ($DryRun) {
    [pscustomobject]@{
        action = "repair_force_multiplier_control_plane"
        workspace_root = $WorkspaceRoot
        service_name = $ServiceName
        service_xml_source = $ServiceXmlSource
        service_xml_target = $ServiceXmlTarget
        winsw_source = $WinSWSource
        service_exe = $ServiceExe
        service_layout = "flat_winsw_service"
        legacy_nested_service_dir = $LegacyNestedServiceDir
        scheduled_task = "ETA-ThreeAI-Sync"
        task_registrar = $RegisterCodexTasks
        python = $PythonExe
        starts_service = [bool]$Start
        restarts_service = [bool]$RestartService
        broker_order_actions = $false
    } | Format-List
    return
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Administrator rights are required to repair $ServiceName. Use repair_force_multiplier_control_plane_admin.cmd or rerun from an elevated shell."
}

New-Item -ItemType Directory -Force -Path $ServicesRoot, $StateDir, $LogDir | Out-Null
Copy-Item -LiteralPath $ServiceXmlSource -Destination $ServiceXmlTarget -Force
Update-FmStatusServiceXmlExecutable -XmlPath $ServiceXmlTarget -PythonExe $PythonExe
Copy-Item -LiteralPath $WinSWSource -Destination $ServiceExe -Force

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    & $ServiceExe install 2>&1 | Write-Host
    if ($LASTEXITCODE -ne 0) {
        throw "WinSW install failed for $ServiceName with exit code $LASTEXITCODE"
    }
    Write-Host "OK: Installed $ServiceName WinSW service." -ForegroundColor Green
} else {
    Write-Host "OK: $ServiceName already registered; refreshed XML and service executable." -ForegroundColor Green
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $RegisterCodexTasks `
    -InstallDir $EtaEngineRoot `
    -StateDir $StateDir `
    -LogDir $LogDir `
    -PythonExe $PythonExe
if ($LASTEXITCODE -ne 0) {
    throw "Force Multiplier task registration failed with exit code $LASTEXITCODE"
}

if ($RestartService) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Stop-SafeFmStatusPortOwner -Port 8422
    Start-Service -Name $ServiceName
    Write-Host "OK: Restarted $ServiceName." -ForegroundColor Green
} elseif ($Start) {
    Stop-SafeFmStatusPortOwner -Port 8422
    Start-Service -Name $ServiceName
    Write-Host "OK: Started $ServiceName." -ForegroundColor Green
}

& $PythonExe -m eta_engine.scripts.force_multiplier_health --json-out --quiet
& $PythonExe -m eta_engine.scripts.vps_ops_hardening_audit --json-out

Write-Host "OK: Force Multiplier control-plane repair completed." -ForegroundColor Green
Write-Host "    Service: $ServiceName"
Write-Host "    Task:    ETA-ThreeAI-Sync"
Write-Host "    Audit:   $StateDir\vps_ops_hardening_latest.json"
