[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Start
)

$ErrorActionPreference = "Stop"

$serviceName = "FirmCommandCenter"
$workspaceRoot = "C:\EvolutionaryTradingAlgo"
$serviceRoot = Join-Path $workspaceRoot "firm_command_center"
$projectRoot = Join-Path $serviceRoot "eta_engine"
$serviceXmlPath = Join-Path $serviceRoot "services\FirmCommandCenter.xml"
$pyprojectPath = Join-Path $projectRoot "pyproject.toml"
$lockPath = Join-Path $projectRoot "uv.lock"
$defaultRuntimePython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$fallbackUv = "C:\Python314\Scripts\uv.exe"
$adminRepairCommand = ".\eta_engine\deploy\scripts\repair_firm_command_center_env_admin.cmd"

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

function Resolve-UvExecutable {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uvCommand) {
        return $uvCommand.Source
    }
    if (Test-Path -LiteralPath $fallbackUv) {
        return $fallbackUv
    }
    throw "Unable to locate uv. Expected it on PATH or at $fallbackUv"
}

function Get-ServiceEnvironment {
    param([xml]$ServiceXml)

    $environment = @{}
    foreach ($node in @($ServiceXml.service.env)) {
        if ($node.name) {
            $environment[[string]$node.name] = [string]$node.value
        }
    }
    return $environment
}

function Get-ServiceImportProbe {
    param([xml]$ServiceXml)

    $serviceArguments = if ($ServiceXml.service.arguments) { [string]$ServiceXml.service.arguments } else { "" }
    $importModules = New-Object System.Collections.Generic.List[string]

    if ($serviceArguments -match '(?i)-m\s+uvicorn\s+([A-Za-z0-9_\.]+):') {
        $importModules.Add("uvicorn")
        $importModules.Add($Matches[1])
    } elseif ($serviceArguments -match '(?i)-m\s+([A-Za-z0-9_\.]+)') {
        $importModules.Add($Matches[1])
    } else {
        $importModules.Add("eta_engine.deploy.scripts.dashboard_api")
    }

    $moduleStatements = @($importModules | Select-Object -Unique | ForEach-Object {
        "importlib.import_module('{0}')" -f $_
    })
    $probeCommand = "import importlib; {0}; print('import OK')" -f ($moduleStatements -join "; ")

    return [pscustomobject]@{
        service_arguments = $serviceArguments
        import_modules = @($importModules | Select-Object -Unique)
        probe_command = $probeCommand
    }
}

function Invoke-ServicePythonProbe {
    param(
        [string]$PythonPath,
        [string]$WorkingDirectory,
        [hashtable]$ServiceEnvironment,
        [string]$ProbeCommand
    )

    if (-not (Test-Path -LiteralPath $PythonPath)) {
        return [pscustomobject]@{
            ok = $false
            detail = "runtime_python_missing"
            missing_module = $null
        }
    }

    $restore = @{}
    try {
        foreach ($entry in $ServiceEnvironment.GetEnumerator()) {
            $name = [string]$entry.Key
            $restore[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        }

        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $PythonPath
        $startInfo.Arguments = "-c `"$ProbeCommand`""
        $startInfo.WorkingDirectory = $WorkingDirectory
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        foreach ($entry in $ServiceEnvironment.GetEnumerator()) {
            $startInfo.Environment[[string]$entry.Key] = [string]$entry.Value
        }

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        $exitCode = $process.ExitCode
    } finally {
        foreach ($entry in $restore.GetEnumerator()) {
            [Environment]::SetEnvironmentVariable([string]$entry.Key, $entry.Value, "Process")
        }
    }

    $detail = (($stdout, $stderr | Where-Object { $_ }) -join "`n").Trim()
    if ($exitCode -eq 0 -and $detail -eq "import OK") {
        return [pscustomobject]@{
            ok = $true
            detail = "import OK"
            missing_module = $null
        }
    }

    $missingModule = $null
    if ($detail -match "No module named '([^']+)'") {
        $missingModule = $Matches[1]
    }

    return [pscustomobject]@{
        ok = $false
        detail = if ($detail) { $detail } else { "import_probe_failed" }
        missing_module = $missingModule
    }
}

Assert-CanonicalEtaPath -Path $workspaceRoot
Assert-CanonicalEtaPath -Path $serviceRoot
Assert-CanonicalEtaPath -Path $projectRoot
Assert-CanonicalEtaPath -Path $serviceXmlPath
Assert-CanonicalEtaPath -Path $pyprojectPath
Assert-CanonicalEtaPath -Path $lockPath

if (-not (Test-Path -LiteralPath $serviceXmlPath)) {
    throw "FirmCommandCenter service XML not found at $serviceXmlPath"
}
if (-not (Test-Path -LiteralPath $pyprojectPath)) {
    throw "FirmCommandCenter project pyproject not found at $pyprojectPath"
}
if (-not (Test-Path -LiteralPath $lockPath)) {
    throw "FirmCommandCenter uv.lock not found at $lockPath"
}

[xml]$serviceXml = Get-Content $serviceXmlPath -Raw
$runtimePython = [string]$serviceXml.service.executable
if (-not $runtimePython) {
    $runtimePython = $defaultRuntimePython
}
$workingDirectory = [string]$serviceXml.service.workingdirectory
if (-not $workingDirectory) {
    $workingDirectory = $workspaceRoot
}

Assert-CanonicalEtaPath -Path $runtimePython
Assert-CanonicalEtaPath -Path $workingDirectory

$serviceEnvironment = Get-ServiceEnvironment -ServiceXml $serviceXml
$serviceImportProbe = Get-ServiceImportProbe -ServiceXml $serviceXml
$uvExecutable = Resolve-UvExecutable
$probeBefore = Invoke-ServicePythonProbe -PythonPath $runtimePython -WorkingDirectory $workingDirectory -ServiceEnvironment $serviceEnvironment -ProbeCommand $serviceImportProbe.probe_command

if ($DryRun) {
    [pscustomobject]@{
        service_name = $serviceName
        runtime_python = $runtimePython
        runtime_python_exists = (Test-Path -LiteralPath $runtimePython)
        project_root = $projectRoot
        working_directory = $workingDirectory
        pyproject = $pyprojectPath
        lockfile = $lockPath
        uv_executable = $uvExecutable
        service_arguments = $serviceImportProbe.service_arguments
        import_probe_modules = @($serviceImportProbe.import_modules)
        probe_command = $serviceImportProbe.probe_command
        import_probe_ok = [bool]$probeBefore.ok
        import_probe_detail = [string]$probeBefore.detail
        missing_module = [string]$probeBefore.missing_module
        sync_command = "$uvExecutable sync --locked"
        repair_command = $adminRepairCommand
        start_service = $Start.IsPresent
    } | Format-List
    return
}

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Administrator rights are required to repair $serviceName. Use repair_firm_command_center_env_admin.cmd or rerun from an elevated shell."
}

$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne "Stopped") {
    Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
    $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
}

Push-Location $projectRoot
try {
    & $uvExecutable sync --locked
    if ($LASTEXITCODE -ne 0) {
        throw "uv sync --locked failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $runtimePython)) {
    throw "Runtime python still missing after sync at $runtimePython"
}

$probeAfter = Invoke-ServicePythonProbe -PythonPath $runtimePython -WorkingDirectory $workingDirectory -ServiceEnvironment $serviceEnvironment -ProbeCommand $serviceImportProbe.probe_command
if (-not $probeAfter.ok) {
    throw "FirmCommandCenter import probe still failing after sync: $($probeAfter.detail)"
}

if ($Start) {
    Start-Service -Name $serviceName
}

Write-Host "OK: Repaired '$serviceName' environment via uv sync."
Write-Host "    Python : $runtimePython"
Write-Host "    Project: $projectRoot"
Write-Host "    Probe  : $($probeAfter.detail)"
if ($Start) {
    Write-Host "    Service: Start requested"
}
